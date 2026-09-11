SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN receipts_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN receipts_consent_version text, ADD COLUMN receipts_consented_at timestamptz;
CREATE TABLE receipt_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  author_user_id uuid NOT NULL REFERENCES users, account_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'collecting' CHECK(state IN ('collecting','processing','ready','failed','cancelled')),
  version integer NOT NULL DEFAULT 1 CHECK(version>0), source_sent_at timestamptz NOT NULL,
  timezone_snapshot text NOT NULL, model text NOT NULL, prompt_version text NOT NULL DEFAULT 'receipt-v1',
  categories_snapshot jsonb, result jsonb, reply jsonb, error_code text, response_id text,
  input_tokens integer, output_tokens integer, lease_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
  UNIQUE(workspace_id,id), FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id)
);
CREATE UNIQUE INDEX one_active_receipt ON receipt_batches(author_user_id) WHERE state IN ('collecting','processing');
CREATE TABLE receipt_files (
  id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES workspaces, author_user_id uuid NOT NULL REFERENCES users,
  batch_id uuid NOT NULL, bot_id bigint NOT NULL, update_id bigint NOT NULL,
  telegram_file_id text NOT NULL, mime_type text NOT NULL CHECK(mime_type IN ('application/pdf','image/jpeg','image/png')),
  sha256 text NOT NULL, size_bytes bigint NOT NULL CHECK(size_bytes>0 AND size_bytes<=15728640),
  page_count integer NOT NULL CHECK(page_count BETWEEN 1 AND 10),
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
  UNIQUE(bot_id,update_id), FOREIGN KEY(workspace_id,batch_id) REFERENCES receipt_batches(workspace_id,id)
);
CREATE INDEX receipt_files_batch_idx ON receipt_files(workspace_id,batch_id);
CREATE INDEX receipt_files_hash_idx ON receipt_files(workspace_id,sha256);
CREATE TABLE receipt_items (
  workspace_id uuid NOT NULL REFERENCES workspaces, author_user_id uuid NOT NULL REFERENCES users,
  batch_id uuid NOT NULL, line_no integer NOT NULL CHECK(line_no>0), name text NOT NULL CHECK(length(name)<=160),
  quantity numeric(20,6), unit_price numeric(20,6), line_total numeric(20,6), discount numeric(20,6),
  PRIMARY KEY(batch_id,line_no), FOREIGN KEY(workspace_id,batch_id) REFERENCES receipt_batches(workspace_id,id),
  CHECK(quantity IS NULL OR quantity>0), CHECK(unit_price IS NULL OR unit_price>=0),
  CHECK(line_total IS NULL OR line_total>=0), CHECK(discount IS NULL OR discount>=0)
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['receipt_batches','receipt_files','receipt_items'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY own ON %I USING(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces)) WITH CHECK(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces))',t);
  END LOOP;
END $$;
ALTER TABLE operation_drafts ADD COLUMN receipt_batch_id uuid,
  ADD COLUMN merchant text CHECK(length(merchant)<=200), ADD COLUMN receipt_currency text,
  ADD COLUMN payment_confirmed boolean NOT NULL DEFAULT false,
  ADD COLUMN duplicate_confirmed boolean NOT NULL DEFAULT false,
  ADD COLUMN receipt_edit_field text CHECK(receipt_edit_field IN ('amount','date','merchant','currency')),
  ADD FOREIGN KEY(workspace_id,receipt_batch_id) REFERENCES receipt_batches(workspace_id,id);
CREATE UNIQUE INDEX one_draft_per_receipt ON operation_drafts(receipt_batch_id) WHERE receipt_batch_id IS NOT NULL;
ALTER TABLE operation_revisions ADD COLUMN merchant text CHECK(length(merchant)<=200);
ALTER TABLE operation_revisions DROP CONSTRAINT operation_revisions_source_kind_check;
ALTER TABLE operation_revisions ADD CHECK(source_kind IN ('manual','receipt'));
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
  SELECT responsible_membership_id,currency INTO m,c FROM accounts WHERE id=d.account_id AND workspace_id=d.workspace_id;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,current_revision_id,source_draft_id,idempotency_key)
    VALUES(d.workspace_id,actor_user_id(),m,r,d.id,d.id::text) RETURNING id INTO o;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,merchant,source_kind)
    VALUES(r,d.workspace_id,o,d.account_id,d.category_id,d.amount,c,d.description,d.occurred_on,d.timezone_snapshot,d.merchant,CASE WHEN d.receipt_batch_id IS NULL THEN 'manual' ELSE 'receipt' END);
  INSERT INTO journal_entries(workspace_id,revision_id,effective_on,idempotency_key)
    VALUES(d.workspace_id,r,d.occurred_on,d.id::text) RETURNING id INTO e;
  INSERT INTO postings(workspace_id,entry_id,line_no,account_id,currency,delta) VALUES(d.workspace_id,e,1,d.account_id,c,-d.amount);
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(d.workspace_id,actor_user_id(),o,'expense_created');
  UPDATE operation_drafts SET state='saved' WHERE id=d.id;
  RETURN o;
END $$;


CREATE OR REPLACE FUNCTION change_category(action_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path=balans,pg_temp AS $$
DECLARE a category_actions; o operations; r operation_revisions; new_id uuid:=gen_random_uuid(); f uuid;
BEGIN
  SELECT * INTO a FROM category_actions WHERE id=action_id AND author_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR a.expires_at<=now() OR a.consumed_at IS NOT NULL THEN RAISE EXCEPTION 'Action unavailable'; END IF;
  SELECT * INTO o FROM operations WHERE id=a.operation_id AND created_by_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR o.current_revision_id<>a.expected_revision_id THEN RAISE EXCEPTION 'Revision conflict'; END IF;
  SELECT * INTO r FROM operation_revisions WHERE id=o.current_revision_id;
  UPDATE category_actions SET consumed_at=now() WHERE id=a.id;
  IF r.category_id=a.category_id THEN RETURN NULL; END IF;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,source_kind,merchant)
    VALUES(new_id,r.workspace_id,r.operation_id,r.revision_no+1,r.account_id,a.category_id,r.amount,r.currency,r.description,r.occurred_on,r.timezone_snapshot,r.source_kind,r.merchant);
  UPDATE operations SET current_revision_id=new_id WHERE id=o.id;
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(o.workspace_id,actor_user_id(),o.id,'category_changed');
  INSERT INTO category_feedback(workspace_id,author_user_id,operation_id,old_category_id,new_category_id,description,expected_revision_id)
    VALUES(o.workspace_id,actor_user_id(),o.id,r.category_id,a.category_id,r.description,new_id) RETURNING id INTO f;
  RETURN f;
END $$;
ALTER TABLE receipt_files ADD COLUMN telegram_kind text NOT NULL DEFAULT 'document' CHECK(telegram_kind IN ('photo','document'));
