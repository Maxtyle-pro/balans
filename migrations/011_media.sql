SET search_path=balans,pg_catalog;
ALTER TABLE receipt_batches ADD COLUMN parent_batch_id uuid REFERENCES receipt_batches;
ALTER TABLE receipt_batches ALTER COLUMN prompt_version SET DEFAULT 'receipt-v2';
ALTER TABLE operation_drafts ADD COLUMN source_type text CHECK(source_type IN ('receipt','screenshot','terminal')),ADD COLUMN external_reference_hash text;
ALTER TABLE operation_revisions ADD COLUMN external_reference_hash text;
ALTER TABLE operation_revisions DROP CONSTRAINT operation_revisions_source_kind_check;
ALTER TABLE operation_revisions ADD CHECK(source_kind IN ('manual','receipt','voice','screenshot','terminal'));
CREATE TABLE media_queues(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,source_batch_id uuid NOT NULL UNIQUE REFERENCES receipt_batches,items jsonb NOT NULL,state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','done','cancelled')),version integer NOT NULL DEFAULT 1,selected_index integer,edit_field text,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours');
CREATE UNIQUE INDEX one_media_queue ON media_queues(author_user_id) WHERE state='active';
ALTER TABLE media_queues ENABLE ROW LEVEL SECURITY;ALTER TABLE media_queues FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON media_queues USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());

CREATE OR REPLACE FUNCTION save_expense(draft_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE d operation_drafts; original operations; prior operation_revisions; parent operation_revisions;
 o uuid; r uuid:=gen_random_uuid(); e uuid; m uuid; currency_code text; old_line record; ordinal integer:=1; refunded numeric; source text;
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 SELECT * INTO d FROM operation_drafts WHERE id=draft_id AND author_user_id=actor_user_id() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Черновик недоступен'; END IF;
 IF d.result_operation_id IS NOT NULL THEN RETURN d.result_operation_id; END IF;
 SELECT id INTO o FROM operations WHERE source_draft_id=d.id;
 IF FOUND THEN RETURN o; END IF;
 IF d.state<>'pending' OR d.step<>'confirm' OR d.expires_at<=now() OR d.amount IS NULL OR d.description IS NULL OR d.occurred_on IS NULL OR d.finance_edit_field IS NOT NULL
 OR (d.kind='expense' AND d.category_id IS NULL) THEN RAISE EXCEPTION 'Черновик не завершён или истёк'; END IF;
 IF d.kind='income' AND EXISTS(SELECT 1 FROM workspaces WHERE id=d.workspace_id AND kind='shared') THEN RAISE EXCEPTION 'Приход общего бюджета требует сверки: /claim'; END IF;
 IF d.occurred_on>(now() AT TIME ZONE d.timezone_snapshot)::date THEN RAISE EXCEPTION 'Будущая дата недопустима'; END IF;
 SELECT responsible_membership_id,currency INTO m,currency_code FROM accounts WHERE id=d.account_id AND workspace_id=d.workspace_id;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM memberships WHERE id=m AND user_id=actor_user_id() AND status='active') THEN RAISE EXCEPTION 'Счёт недоступен'; END IF;
 IF d.kind='transfer' AND (d.destination_account_id IS NULL OR d.destination_account_id=d.account_id OR NOT EXISTS(SELECT 1 FROM accounts WHERE id=d.destination_account_id AND workspace_id=d.workspace_id AND currency=currency_code AND responsible_membership_id=m)) THEN RAISE EXCEPTION 'Выберите другой счёт той же валюты'; END IF;
 IF d.edit_operation_id IS NOT NULL THEN
  SELECT * INTO original FROM operations WHERE id=d.edit_operation_id AND created_by_user_id=actor_user_id() AND workspace_id=d.workspace_id FOR UPDATE;
  IF NOT FOUND OR original.state<>'active' OR original.current_revision_id IS DISTINCT FROM d.expected_revision_id OR original.kind<>d.kind THEN RAISE EXCEPTION 'Операция изменилась или недоступна. Откройте её заново'; END IF;
  SELECT * INTO prior FROM operation_revisions WHERE id=original.current_revision_id;
  o:=original.id;ordinal:=prior.revision_no+1;
  IF d.cancel_operation AND coalesce(length(trim(d.change_reason)),0)=0 THEN RAISE EXCEPTION 'Укажите причину отмены'; END IF;
 END IF;
 IF d.kind='expense' AND d.edit_operation_id IS NOT NULL THEN
  SELECT coalesce(sum(r.amount),0) INTO refunded FROM operations op JOIN operation_revisions r ON r.id=op.current_revision_id WHERE op.kind='refund' AND op.state='active' AND r.refund_of=o;
  IF (d.cancel_operation AND refunded>0) OR d.amount<refunded THEN RAISE EXCEPTION 'Сначала отмените или исправьте связанные возвраты'; END IF;
  IF EXISTS(SELECT 1 FROM operations op JOIN operation_revisions r ON r.id=op.current_revision_id WHERE op.kind='refund' AND op.state='active' AND r.refund_of=o AND r.occurred_on<d.occurred_on) THEN RAISE EXCEPTION 'Покупка не может быть позже её возврата'; END IF;
 END IF;
 IF d.kind='refund' AND NOT d.cancel_operation THEN
  SELECT r.* INTO parent FROM operations op JOIN operation_revisions r ON r.id=op.current_revision_id WHERE op.id=d.refund_of AND op.workspace_id=d.workspace_id AND op.state='active' AND op.kind='expense' FOR UPDATE OF op;
  IF NOT FOUND OR parent.currency<>currency_code OR d.occurred_on<parent.occurred_on THEN RAISE EXCEPTION 'Исходная покупка недоступна или дата возврата раньше покупки'; END IF;
  IF d.edit_operation_id IS NOT NULL AND prior.refund_of IS DISTINCT FROM d.refund_of THEN RAISE EXCEPTION 'Нельзя изменить исходную покупку возврата'; END IF;
  SELECT coalesce(sum(r.amount),0) INTO refunded FROM operations op JOIN operation_revisions r ON r.id=op.current_revision_id WHERE op.kind='refund' AND op.state='active' AND r.refund_of=d.refund_of AND (o IS NULL OR op.id<>o);
  IF refunded+d.amount>parent.amount THEN RAISE EXCEPTION 'Возвраты превышают сумму покупки'; END IF;
  d.category_id:=parent.category_id;
 END IF;
 IF d.kind='opening' AND NOT d.cancel_operation AND EXISTS(SELECT 1 FROM operations op JOIN operation_revisions rev ON rev.id=op.current_revision_id WHERE op.state='active' AND op.kind<>'opening' AND (rev.account_id=d.account_id OR rev.destination_account_id=d.account_id) AND rev.occurred_on<d.occurred_on) THEN RAISE EXCEPTION 'Начальная дата должна быть не позже первой операции счёта'; END IF;
 IF d.kind='opening' AND NOT d.cancel_operation AND EXISTS(SELECT 1 FROM operations op JOIN operation_revisions r ON r.id=op.current_revision_id WHERE op.kind='opening' AND op.state='active' AND r.account_id=d.account_id AND (o IS NULL OR op.id<>o)) THEN RAISE EXCEPTION 'Начальный остаток уже задан. Исправьте существующую запись'; END IF;
 IF d.edit_operation_id IS NULL THEN
  IF d.receipt_batch_id IS NOT NULL AND (NOT d.payment_confirmed OR d.receipt_currency IS DISTINCT FROM 'RUB' OR d.receipt_edit_field IS NOT NULL) THEN RAISE EXCEPTION 'Подтвердите оплату и завершите проверку чека'; END IF;
  IF d.voice_edit_field IS NOT NULL THEN RAISE EXCEPTION 'Завершите исправление голоса'; END IF;
  IF (d.receipt_batch_id IS NOT NULL OR d.voice_job_id IS NOT NULL) AND NOT d.duplicate_confirmed AND EXISTS(SELECT 1 FROM operations op JOIN operation_revisions rev ON rev.id=op.current_revision_id WHERE op.workspace_id=d.workspace_id AND op.kind=d.kind AND op.state='active' AND ((rev.amount=d.amount AND rev.occurred_on=d.occurred_on) OR (d.external_reference_hash IS NOT NULL AND rev.external_reference_hash=d.external_reference_hash))) THEN RAISE EXCEPTION 'Подтвердите возможный дубль'; END IF;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,kind,current_revision_id,source_draft_id,idempotency_key) VALUES(d.workspace_id,actor_user_id(),m,d.kind,r,d.id,d.id::text) RETURNING id INTO o;
 END IF;
 source:=CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.source_kind WHEN d.voice_job_id IS NOT NULL THEN 'voice' WHEN d.receipt_batch_id IS NOT NULL THEN coalesce(d.source_type,'receipt') ELSE 'manual' END;
 INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,merchant,source_kind,destination_account_id,refund_of,opening_negative,change_reason,external_reference_hash)
 VALUES(r,d.workspace_id,o,ordinal,d.account_id,d.category_id,d.amount,currency_code,d.description,d.occurred_on,d.timezone_snapshot,CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.merchant ELSE d.merchant END,source,d.destination_account_id,d.refund_of,d.opening_negative,d.change_reason,CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.external_reference_hash ELSE d.external_reference_hash END);
 IF d.edit_operation_id IS NOT NULL THEN
  FOR old_line IN SELECT e.effective_on,p.account_id,p.currency,sum(p.delta) AS delta FROM operation_revisions rev JOIN journal_entries e ON e.revision_id=rev.id JOIN postings p ON p.entry_id=e.id WHERE rev.operation_id=o GROUP BY e.effective_on,p.account_id,p.currency HAVING sum(p.delta)<>0 LOOP
   INSERT INTO journal_entries(workspace_id,revision_id,event_kind,effective_on,idempotency_key) VALUES(d.workspace_id,r,'reversal',old_line.effective_on,d.id::text||':'||old_line.account_id::text||':'||old_line.effective_on::text) RETURNING id INTO e;
   INSERT INTO postings VALUES(d.workspace_id,e,1,old_line.account_id,old_line.currency,-old_line.delta);
  END LOOP;
 END IF;
 IF NOT d.cancel_operation THEN
  INSERT INTO journal_entries(workspace_id,revision_id,event_kind,effective_on,idempotency_key) VALUES(d.workspace_id,r,d.kind,d.occurred_on,d.id::text) RETURNING id INTO e;
  INSERT INTO postings VALUES(d.workspace_id,e,1,d.account_id,currency_code,CASE WHEN d.kind IN ('expense','transfer') OR (d.kind='opening' AND d.opening_negative) THEN -d.amount ELSE d.amount END);
  IF d.kind='transfer' THEN INSERT INTO postings VALUES(d.workspace_id,e,2,d.destination_account_id,currency_code,d.amount); END IF;
 END IF;
 UPDATE operations SET current_revision_id=r,state=CASE WHEN d.cancel_operation THEN 'cancelled' ELSE 'active' END,responsible_membership_id=m WHERE id=o;
 INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(d.workspace_id,actor_user_id(),o,CASE WHEN d.cancel_operation THEN 'operation_cancelled' WHEN d.edit_operation_id IS NOT NULL THEN 'operation_changed' WHEN d.kind='expense' THEN 'expense_created' ELSE 'operation_created' END);
 UPDATE operation_drafts SET state='saved',result_operation_id=o WHERE id=d.id;
 RETURN o;
END $$;

CREATE OR REPLACE FUNCTION guard_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE ds document_sets; prior date;
BEGIN
 IF NEW.external_reference_hash IS NULL THEN SELECT external_reference_hash INTO NEW.external_reference_hash FROM operation_revisions WHERE operation_id=NEW.operation_id ORDER BY revision_no DESC LIMIT 1; END IF;
 PERFORM require_open_period(NEW.occurred_on);
 SELECT occurred_on INTO prior FROM operation_revisions WHERE operation_id=NEW.operation_id ORDER BY revision_no DESC LIMIT 1;
 IF FOUND THEN PERFORM require_open_period(prior); END IF;
 SELECT * INTO ds FROM document_sets WHERE id=NEW.operation_id FOR UPDATE;
 IF FOUND THEN
  IF ds.financial_accepted AND NOT ds.correction_approved THEN RAISE EXCEPTION 'Запись принята. Сначала запросите исправление у руководителя: /correction ID | причина'; END IF;
  UPDATE document_sets SET status='unreviewed',financial_accepted=false,correction_requested=false,correction_approved=false,occurred_on=NEW.occurred_on,amount=NEW.amount,category_id=NEW.category_id WHERE id=ds.id;
  INSERT INTO review_audit(workspace_id,set_id,actor_user_id,action) VALUES(ds.workspace_id,ds.id,actor_user_id(),'revision_changed');
 END IF;RETURN NEW;
END $$;

CREATE INDEX revision_external_reference ON operation_revisions(workspace_id,external_reference_hash) WHERE external_reference_hash IS NOT NULL;
