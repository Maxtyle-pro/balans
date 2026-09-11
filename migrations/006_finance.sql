SET search_path=balans,pg_catalog;
ALTER TABLE accounts ADD CONSTRAINT account_name_length CHECK(length(name) BETWEEN 1 AND 80);
CREATE UNIQUE INDEX accounts_name_unique ON accounts(workspace_id,lower(name));
ALTER TABLE user_settings ADD COLUMN default_account_id uuid REFERENCES accounts;
ALTER TABLE operation_drafts ADD COLUMN kind text NOT NULL DEFAULT 'expense' CHECK(kind IN ('expense','income','transfer','refund','opening')),
 ADD COLUMN destination_account_id uuid, ADD COLUMN refund_of uuid,
 ADD COLUMN edit_operation_id uuid, ADD COLUMN expected_revision_id uuid,
 ADD COLUMN cancel_operation boolean NOT NULL DEFAULT false, ADD COLUMN change_reason text CHECK(length(change_reason)<=500),
 ADD COLUMN opening_negative boolean NOT NULL DEFAULT false,
 ADD COLUMN finance_edit_field text CHECK(finance_edit_field IN ('amount','date','description','account','destination','reason')),
 ADD FOREIGN KEY(workspace_id,destination_account_id) REFERENCES accounts(workspace_id,id),
 ADD FOREIGN KEY(workspace_id,refund_of) REFERENCES operations(workspace_id,id),
 ADD FOREIGN KEY(workspace_id,edit_operation_id) REFERENCES operations(workspace_id,id);
ALTER TABLE operations DROP CONSTRAINT operations_kind_check;
ALTER TABLE operations ADD CHECK(kind IN ('expense','income','transfer','refund','opening'));
ALTER TABLE operations DROP CONSTRAINT operations_state_check;
ALTER TABLE operations ADD CHECK(state IN ('active','cancelled'));
ALTER TABLE operation_revisions ALTER COLUMN category_id DROP NOT NULL;
ALTER TABLE operation_revisions ADD COLUMN destination_account_id uuid, ADD COLUMN refund_of uuid,
 ADD COLUMN opening_negative boolean NOT NULL DEFAULT false,
 ADD COLUMN change_reason text CHECK(length(change_reason)<=500),
 ADD FOREIGN KEY(workspace_id,destination_account_id) REFERENCES accounts(workspace_id,id),
 ADD FOREIGN KEY(workspace_id,refund_of) REFERENCES operations(workspace_id,id);
ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_revision_id_key;
ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_event_kind_check;
ALTER TABLE journal_entries ADD CHECK(event_kind IN ('expense','income','transfer','refund','opening','reversal'));
ALTER TABLE postings DROP CONSTRAINT postings_line_no_check;
ALTER TABLE postings ADD CHECK(line_no>0);
ALTER TABLE postings DROP CONSTRAINT postings_delta_check;
ALTER TABLE postings ADD CHECK(delta<>0 AND delta=round(delta,2));
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CHECK(action IN ('expense_created','category_changed','operation_created','operation_changed','operation_cancelled'));
ALTER TABLE operation_drafts ADD COLUMN result_operation_id uuid REFERENCES operations;
CREATE FUNCTION create_account(account_name text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE a uuid; w uuid; m uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 IF length(trim(account_name)) NOT BETWEEN 1 AND 80 THEN RAISE EXCEPTION 'Название счёта: 1–80 символов'; END IF;
 SELECT id INTO w FROM workspaces WHERE owner_user_id=actor_user_id() AND kind='personal';
 IF w IS NULL THEN RAISE EXCEPTION 'Бюджет недоступен'; END IF;
 SELECT id INTO m FROM memberships WHERE workspace_id=w AND user_id=actor_user_id();
 SELECT id INTO a FROM accounts WHERE workspace_id=w AND lower(name)=lower(trim(account_name));
 IF a IS NOT NULL THEN RETURN a; END IF;
 INSERT INTO accounts(workspace_id,responsible_membership_id,name) VALUES(w,m,trim(account_name)) RETURNING id INTO a;
 RETURN a;
END $$;
REVOKE ALL ON FUNCTION create_account(text) FROM PUBLIC;

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
 IF d.occurred_on>(now() AT TIME ZONE d.timezone_snapshot)::date THEN RAISE EXCEPTION 'Будущая дата недопустима'; END IF;
 SELECT responsible_membership_id,currency INTO m,currency_code FROM accounts WHERE id=d.account_id AND workspace_id=d.workspace_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Счёт недоступен'; END IF;
 IF d.kind='transfer' AND (d.destination_account_id IS NULL OR d.destination_account_id=d.account_id OR NOT EXISTS(SELECT 1 FROM accounts WHERE id=d.destination_account_id AND workspace_id=d.workspace_id AND currency=currency_code)) THEN RAISE EXCEPTION 'Выберите другой счёт той же валюты'; END IF;
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
  IF (d.receipt_batch_id IS NOT NULL OR d.voice_job_id IS NOT NULL) AND NOT d.duplicate_confirmed AND EXISTS(SELECT 1 FROM operations op JOIN operation_revisions rev ON rev.id=op.current_revision_id WHERE op.workspace_id=d.workspace_id AND op.kind='expense' AND op.state='active' AND rev.amount=d.amount AND rev.occurred_on=d.occurred_on) THEN RAISE EXCEPTION 'Подтвердите возможный дубль'; END IF;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,kind,current_revision_id,source_draft_id,idempotency_key) VALUES(d.workspace_id,actor_user_id(),m,d.kind,r,d.id,d.id::text) RETURNING id INTO o;
 END IF;
 source:=CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.source_kind WHEN d.voice_job_id IS NOT NULL THEN 'voice' WHEN d.receipt_batch_id IS NOT NULL THEN 'receipt' ELSE 'manual' END;
 INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,merchant,source_kind,destination_account_id,refund_of,opening_negative,change_reason)
 VALUES(r,d.workspace_id,o,ordinal,d.account_id,d.category_id,d.amount,currency_code,d.description,d.occurred_on,d.timezone_snapshot,CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.merchant ELSE d.merchant END,source,d.destination_account_id,d.refund_of,d.opening_negative,d.change_reason);
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
CREATE OR REPLACE FUNCTION check_expense() RETURNS trigger LANGUAGE plpgsql SET search_path=balans,pg_temp AS $$
DECLARE r operation_revisions; v numeric;
BEGIN
 SELECT rev.* INTO r FROM operations o JOIN operation_revisions rev ON rev.id=o.current_revision_id WHERE o.id=NEW.id;
 SELECT sum(p.delta) INTO v FROM journal_entries e JOIN postings p ON p.entry_id=e.id WHERE e.revision_id=r.id;
 IF v IS NULL OR v<>(CASE WHEN NEW.kind='transfer' THEN 0 WHEN NEW.kind='expense' OR (NEW.kind='opening' AND r.opening_negative) THEN -r.amount ELSE r.amount END) THEN RAISE EXCEPTION 'Incomplete financial journal'; END IF;
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION change_category(action_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path=balans,pg_temp AS $$
DECLARE a category_actions; o operations; r operation_revisions; new_id uuid:=gen_random_uuid(); f uuid;
BEGIN
  SELECT * INTO a FROM category_actions WHERE id=action_id AND author_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR a.expires_at<=now() OR a.consumed_at IS NOT NULL THEN RAISE EXCEPTION 'Action unavailable'; END IF;
  SELECT * INTO o FROM operations WHERE id=a.operation_id AND created_by_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR o.state<>'active' OR o.kind<>'expense' OR o.current_revision_id<>a.expected_revision_id THEN RAISE EXCEPTION 'Revision conflict'; END IF;
  SELECT * INTO r FROM operation_revisions WHERE id=o.current_revision_id;
  UPDATE category_actions SET consumed_at=now() WHERE id=a.id;
  IF r.category_id=a.category_id THEN RETURN NULL; END IF;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,source_kind,merchant,destination_account_id,refund_of,opening_negative,change_reason)
    VALUES(new_id,r.workspace_id,r.operation_id,r.revision_no+1,r.account_id,a.category_id,r.amount,r.currency,r.description,r.occurred_on,r.timezone_snapshot,r.source_kind,r.merchant,r.destination_account_id,r.refund_of,r.opening_negative,r.change_reason);
  UPDATE operations SET current_revision_id=new_id WHERE id=o.id;
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(o.workspace_id,actor_user_id(),o.id,'category_changed');
  INSERT INTO category_feedback(workspace_id,author_user_id,operation_id,old_category_id,new_category_id,description,expected_revision_id)
    VALUES(o.workspace_id,actor_user_id(),o.id,r.category_id,a.category_id,r.description,new_id) RETURNING id INTO f;
  RETURN f;
END $$;
