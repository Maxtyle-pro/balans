SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN voice_enabled boolean NOT NULL DEFAULT false,
 ADD COLUMN voice_consented_at timestamptz, ADD COLUMN voice_consent_version text;
CREATE TABLE voice_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
 author_user_id uuid NOT NULL REFERENCES users, account_id uuid NOT NULL,
 source_sent_at timestamptz NOT NULL, timezone_snapshot text NOT NULL,
 state text NOT NULL DEFAULT 'processing' CHECK(state IN ('processing','ready','failed','cancelled')),
 transcript text CHECK(length(transcript)<=6000), result jsonb, reply jsonb, error_code text,
 model text NOT NULL, transcribe_model text NOT NULL, prompt_version text NOT NULL DEFAULT 'voice-v1',
 created_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz NOT NULL DEFAULT now()+interval '3 minutes',
 UNIQUE(workspace_id,id), FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id)
);
CREATE UNIQUE INDEX one_active_voice ON voice_jobs(author_user_id) WHERE state='processing';
ALTER TABLE voice_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON voice_jobs USING(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces)) WITH CHECK(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces));
ALTER TABLE operation_drafts ADD COLUMN voice_job_id uuid,
 ADD COLUMN voice_edit_field text CHECK(voice_edit_field IN ('amount','date','description')),
 ADD FOREIGN KEY(workspace_id,voice_job_id) REFERENCES voice_jobs(workspace_id,id),
 ADD CHECK(voice_job_id IS NULL OR receipt_batch_id IS NULL);
CREATE UNIQUE INDEX one_draft_per_voice ON operation_drafts(voice_job_id) WHERE voice_job_id IS NOT NULL;
ALTER TABLE operation_revisions DROP CONSTRAINT operation_revisions_source_kind_check;
ALTER TABLE operation_revisions ADD CHECK(source_kind IN ('manual','receipt','voice'));

CREATE OR REPLACE FUNCTION save_expense(draft_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = balans, pg_temp AS $$
DECLARE d operation_drafts; o uuid; r uuid:=gen_random_uuid(); e uuid; m uuid; c text;
BEGIN
  -- RLS and explicit author check apply even to callbacks with a substituted UUID.
  SELECT * INTO d FROM operation_drafts WHERE id=draft_id AND author_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Draft unavailable'; END IF;
  SELECT id INTO o FROM operations WHERE source_draft_id=d.id;
  IF FOUND THEN RETURN o; END IF;
  IF d.state<>'pending' OR d.step<>'confirm' OR d.expires_at<=now()
    OR d.amount IS NULL OR d.category_id IS NULL OR d.description IS NULL OR d.occurred_on IS NULL
    THEN RAISE EXCEPTION 'Draft incomplete or expired'; END IF;
  IF d.receipt_batch_id IS NOT NULL THEN
    IF NOT d.payment_confirmed OR d.receipt_currency IS DISTINCT FROM 'RUB' THEN
      RAISE EXCEPTION 'Receipt payment or currency unconfirmed';
    END IF;
    IF d.receipt_edit_field IS NOT NULL THEN RAISE EXCEPTION 'Receipt edit unfinished'; END IF;
    IF NOT d.duplicate_confirmed AND EXISTS(
      SELECT 1 FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id
      WHERE o.workspace_id=d.workspace_id AND r.amount=d.amount AND r.occurred_on=d.occurred_on
    ) THEN RAISE EXCEPTION 'Possible duplicate needs confirmation'; END IF;
  END IF;
  IF d.voice_job_id IS NOT NULL THEN
    IF d.voice_edit_field IS NOT NULL THEN RAISE EXCEPTION 'Voice edit unfinished'; END IF;
    IF NOT d.duplicate_confirmed AND EXISTS(SELECT 1 FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.workspace_id=d.workspace_id AND r.amount=d.amount AND r.occurred_on=d.occurred_on) THEN RAISE EXCEPTION 'Possible duplicate needs confirmation'; END IF;
  END IF;
  SELECT responsible_membership_id,currency INTO m,c FROM accounts WHERE id=d.account_id AND workspace_id=d.workspace_id;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,current_revision_id,source_draft_id,idempotency_key)
    VALUES(d.workspace_id,actor_user_id(),m,r,d.id,d.id::text) RETURNING id INTO o;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,merchant,source_kind)
    VALUES(r,d.workspace_id,o,d.account_id,d.category_id,d.amount,c,d.description,d.occurred_on,d.timezone_snapshot,d.merchant,CASE WHEN d.voice_job_id IS NOT NULL THEN 'voice' WHEN d.receipt_batch_id IS NOT NULL THEN 'receipt' ELSE 'manual' END);
  INSERT INTO journal_entries(workspace_id,revision_id,effective_on,idempotency_key)
    VALUES(d.workspace_id,r,d.occurred_on,d.id::text) RETURNING id INTO e;
  INSERT INTO postings(workspace_id,entry_id,line_no,account_id,currency,delta) VALUES(d.workspace_id,e,1,d.account_id,c,-d.amount);
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(d.workspace_id,actor_user_id(),o,'expense_created');
  UPDATE operation_drafts SET state='saved' WHERE id=d.id;
  RETURN o;
END $$;


