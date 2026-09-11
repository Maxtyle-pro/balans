SET search_path=balans,pg_catalog;
INSERT INTO currencies VALUES('USD',2),('EUR',2) ON CONFLICT DO NOTHING;
ALTER TABLE operation_drafts ADD COLUMN destination_amount numeric(20,2) CHECK(destination_amount>0 AND destination_amount<=999999999999.99);
ALTER TABLE operation_revisions ADD COLUMN destination_amount numeric(20,2),ADD COLUMN base_amount numeric(20,2),ADD COLUMN exchange_rate numeric(24,10),ADD COLUMN exchange_rate_on date,ADD COLUMN rate_source text;
ALTER TABLE document_sets ADD COLUMN currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies;
CREATE TABLE exchange_rates(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,author_user_id uuid NOT NULL REFERENCES users,currency text NOT NULL REFERENCES currencies,base_currency text NOT NULL REFERENCES currencies,rate numeric(24,10) NOT NULL CHECK(rate>0 AND rate<100000000),effective_on date NOT NULL,source text NOT NULL DEFAULT 'manual',UNIQUE(workspace_id,author_user_id,currency,base_currency,effective_on));
ALTER TABLE exchange_rates ENABLE ROW LEVEL SECURITY;ALTER TABLE exchange_rates FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON exchange_rates USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE FUNCTION create_currency_account(account_name text,currency_code text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces;m uuid;identity uuid;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=current_workspace();
 IF w.archived THEN RAISE EXCEPTION 'Бюджет архивирован.';END IF;
 IF NOT EXISTS(SELECT 1 FROM currencies WHERE code=currency_code) THEN RAISE EXCEPTION 'Доступны RUB, USD, EUR.';END IF;
 IF w.kind='shared' AND currency_code<>w.base_currency THEN RAISE EXCEPTION 'Счета общего бюджета используют его валюту.';END IF;
 IF length(btrim(account_name)) NOT BETWEEN 1 AND 80 THEN RAISE EXCEPTION 'Название счёта: 1–80 символов.';END IF;
 SELECT id INTO m FROM memberships WHERE workspace_id=w.id AND user_id=actor_user_id() AND status='active';
 IF m IS NULL THEN RAISE EXCEPTION 'Нет доступа к бюджету.';END IF;
 IF EXISTS(SELECT 1 FROM accounts WHERE workspace_id=w.id AND responsible_membership_id=m AND lower(name)=lower(btrim(account_name))) THEN RAISE EXCEPTION 'Счёт с таким названием уже есть.';END IF;
 INSERT INTO accounts(workspace_id,responsible_membership_id,name,currency) VALUES(w.id,m,btrim(account_name),currency_code) RETURNING id INTO identity;
 RETURN identity;
END $$;
CREATE FUNCTION set_revision_currency() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE d operation_drafts;rate exchange_rates;prior operation_revisions;base text;
BEGIN
 SELECT * INTO d FROM operation_drafts WHERE result_operation_id=NEW.operation_id OR (id=(SELECT source_draft_id FROM operations WHERE id=NEW.operation_id)) ORDER BY created_at DESC LIMIT 1;
 SELECT base_currency INTO base FROM workspaces WHERE id=NEW.workspace_id;
 SELECT * INTO prior FROM operation_revisions WHERE operation_id=NEW.operation_id AND revision_no<NEW.revision_no ORDER BY revision_no DESC LIMIT 1;
 IF NEW.currency=base THEN NEW.base_amount:=NEW.amount;NEW.exchange_rate:=1;NEW.exchange_rate_on:=NEW.occurred_on;NEW.rate_source:='same_currency';
 ELSIF prior.id IS NOT NULL AND prior.currency=NEW.currency AND prior.occurred_on=NEW.occurred_on THEN
  NEW.exchange_rate:=prior.exchange_rate;NEW.exchange_rate_on:=prior.exchange_rate_on;NEW.rate_source:=prior.rate_source;NEW.base_amount:=round(NEW.amount*prior.exchange_rate,2);
 ELSE
  SELECT * INTO rate FROM exchange_rates WHERE currency=NEW.currency AND base_currency=base AND effective_on<=NEW.occurred_on ORDER BY effective_on DESC LIMIT 1;
  IF FOUND THEN NEW.exchange_rate:=rate.rate;NEW.exchange_rate_on:=rate.effective_on;NEW.rate_source:=rate.source;NEW.base_amount:=round(NEW.amount*rate.rate,2);END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER revision_currency BEFORE INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION set_revision_currency();
CREATE FUNCTION set_document_currency() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN
 SELECT coalesce((SELECT r.currency FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=NEW.id),(SELECT base_currency FROM workspaces WHERE id=NEW.workspace_id)) INTO NEW.currency;RETURN NEW;
END $$;
CREATE TRIGGER document_currency BEFORE INSERT ON document_sets FOR EACH ROW EXECUTE FUNCTION set_document_currency();
REVOKE ALL ON FUNCTION create_currency_account(text,text),set_revision_currency(),set_document_currency() FROM PUBLIC;

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
 IF d.kind='transfer' AND (d.destination_account_id IS NULL OR d.destination_account_id=d.account_id OR NOT EXISTS(SELECT 1 FROM accounts WHERE id=d.destination_account_id AND workspace_id=d.workspace_id AND responsible_membership_id=m AND (currency=currency_code OR d.destination_amount IS NOT NULL))) THEN RAISE EXCEPTION 'Выберите другой счёт той же валюты'; END IF;
 IF d.kind='transfer' AND d.destination_amount IS NOT NULL AND d.destination_amount<>d.amount AND EXISTS(SELECT 1 FROM accounts WHERE id=d.destination_account_id AND currency=currency_code) THEN RAISE EXCEPTION 'Перевод в одной валюте требует равных сумм. Комиссия — отдельный расход.';END IF;
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
  IF d.receipt_batch_id IS NOT NULL AND (NOT d.payment_confirmed OR d.receipt_currency IS DISTINCT FROM currency_code OR d.receipt_edit_field IS NOT NULL) THEN RAISE EXCEPTION 'Подтвердите оплату и завершите проверку чека'; END IF;
  IF d.voice_edit_field IS NOT NULL THEN RAISE EXCEPTION 'Завершите исправление голоса'; END IF;
  IF (d.receipt_batch_id IS NOT NULL OR d.voice_job_id IS NOT NULL) AND NOT d.duplicate_confirmed AND EXISTS(SELECT 1 FROM operations op JOIN operation_revisions rev ON rev.id=op.current_revision_id WHERE op.workspace_id=d.workspace_id AND op.kind=d.kind AND op.state='active' AND ((rev.amount=d.amount AND rev.occurred_on=d.occurred_on) OR (d.external_reference_hash IS NOT NULL AND rev.external_reference_hash=d.external_reference_hash))) THEN RAISE EXCEPTION 'Подтвердите возможный дубль'; END IF;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,kind,current_revision_id,source_draft_id,idempotency_key) VALUES(d.workspace_id,actor_user_id(),m,d.kind,r,d.id,d.id::text) RETURNING id INTO o;
 END IF;
 source:=CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.source_kind WHEN d.voice_job_id IS NOT NULL THEN 'voice' WHEN d.receipt_batch_id IS NOT NULL THEN coalesce(d.source_type,'receipt') ELSE 'manual' END;
 INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,merchant,source_kind,destination_account_id,refund_of,opening_negative,destination_amount,change_reason,external_reference_hash)
 VALUES(r,d.workspace_id,o,ordinal,d.account_id,d.category_id,d.amount,currency_code,d.description,d.occurred_on,d.timezone_snapshot,CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.merchant ELSE d.merchant END,source,d.destination_account_id,d.refund_of,d.opening_negative,CASE WHEN d.kind='transfer' THEN coalesce(d.destination_amount,d.amount) END,d.change_reason,CASE WHEN d.edit_operation_id IS NOT NULL THEN prior.external_reference_hash ELSE d.external_reference_hash END);
 IF d.edit_operation_id IS NOT NULL THEN
  FOR old_line IN SELECT e.effective_on,p.account_id,p.currency,sum(p.delta) AS delta FROM operation_revisions rev JOIN journal_entries e ON e.revision_id=rev.id JOIN postings p ON p.entry_id=e.id WHERE rev.operation_id=o GROUP BY e.effective_on,p.account_id,p.currency HAVING sum(p.delta)<>0 LOOP
   INSERT INTO journal_entries(workspace_id,revision_id,event_kind,effective_on,idempotency_key) VALUES(d.workspace_id,r,'reversal',old_line.effective_on,d.id::text||':'||old_line.account_id::text||':'||old_line.effective_on::text) RETURNING id INTO e;
   INSERT INTO postings VALUES(d.workspace_id,e,1,old_line.account_id,old_line.currency,-old_line.delta);
  END LOOP;
 END IF;
 IF NOT d.cancel_operation THEN
  INSERT INTO journal_entries(workspace_id,revision_id,event_kind,effective_on,idempotency_key) VALUES(d.workspace_id,r,d.kind,d.occurred_on,d.id::text) RETURNING id INTO e;
  INSERT INTO postings VALUES(d.workspace_id,e,1,d.account_id,currency_code,CASE WHEN d.kind IN ('expense','transfer') OR (d.kind='opening' AND d.opening_negative) THEN -d.amount ELSE d.amount END);
  IF d.kind='transfer' THEN INSERT INTO postings VALUES(d.workspace_id,e,2,d.destination_account_id,(SELECT currency FROM accounts WHERE id=d.destination_account_id),coalesce(d.destination_amount,d.amount)); END IF;
 END IF;
 UPDATE operations SET current_revision_id=r,state=CASE WHEN d.cancel_operation THEN 'cancelled' ELSE 'active' END,responsible_membership_id=m WHERE id=o;
 INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(d.workspace_id,actor_user_id(),o,CASE WHEN d.cancel_operation THEN 'operation_cancelled' WHEN d.edit_operation_id IS NOT NULL THEN 'operation_changed' WHEN d.kind='expense' THEN 'expense_created' ELSE 'operation_created' END);
 UPDATE operation_drafts SET state='saved',result_operation_id=o WHERE id=d.id;
 RETURN o;
END $$;
CREATE OR REPLACE FUNCTION check_expense() RETURNS trigger LANGUAGE plpgsql SET search_path=balans,pg_temp AS $$
DECLARE r operation_revisions;v numeric;BEGIN
 SELECT rev.* INTO r FROM operations o JOIN operation_revisions rev ON rev.id=o.current_revision_id WHERE o.id=NEW.id;
 IF NEW.kind='transfer' THEN
  IF (SELECT count(*) FROM journal_entries e JOIN postings p ON p.entry_id=e.id WHERE e.revision_id=r.id)<>2 OR NOT EXISTS(SELECT 1 FROM journal_entries e JOIN postings p ON p.entry_id=e.id WHERE e.revision_id=r.id AND p.account_id=r.account_id AND p.currency=r.currency AND p.delta=-r.amount) OR NOT EXISTS(SELECT 1 FROM journal_entries e JOIN postings p ON p.entry_id=e.id JOIN accounts a ON a.id=p.account_id WHERE e.revision_id=r.id AND p.account_id=r.destination_account_id AND p.currency=a.currency AND p.delta=coalesce(r.destination_amount,r.amount)) THEN RAISE EXCEPTION 'Incomplete currency transfer journal';END IF;
 ELSE
  SELECT sum(p.delta) INTO v FROM journal_entries e JOIN postings p ON p.entry_id=e.id WHERE e.revision_id=r.id AND p.currency=r.currency;
  IF v IS NULL OR v<>(CASE WHEN NEW.kind='expense' OR (NEW.kind='opening' AND r.opening_negative) THEN -r.amount ELSE r.amount END) THEN RAISE EXCEPTION 'Incomplete financial journal';END IF;
 END IF;RETURN NULL;
END $$;
CREATE FUNCTION default_shared_account_currency() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN IF EXISTS(SELECT 1 FROM workspaces WHERE id=NEW.workspace_id AND kind='shared') THEN SELECT base_currency INTO NEW.currency FROM workspaces WHERE id=NEW.workspace_id;END IF;RETURN NEW;END $$;
CREATE TRIGGER account_currency BEFORE INSERT ON accounts FOR EACH ROW EXECUTE FUNCTION default_shared_account_currency();
CREATE FUNCTION create_currency_workspace(workspace_name text,currency_code text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE identity uuid;BEGIN
 IF NOT EXISTS(SELECT 1 FROM currencies WHERE code=currency_code) THEN RAISE EXCEPTION 'Доступны RUB, USD, EUR.';END IF;
 identity:=create_workspace(workspace_name);
 UPDATE workspaces SET base_currency=currency_code WHERE id=identity;
 UPDATE accounts SET currency=currency_code WHERE workspace_id=identity;
 RETURN identity;
END $$;
REVOKE ALL ON FUNCTION default_shared_account_currency(),create_currency_workspace(text,text) FROM PUBLIC;
ALTER TABLE spending_budgets ADD COLUMN currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies;
DROP INDEX budget_per_category;
CREATE UNIQUE INDEX budget_per_category ON spending_budgets(workspace_id,author_user_id,category_id,currency) NULLS NOT DISTINCT;

ALTER TABLE operation_drafts DROP CONSTRAINT operation_drafts_finance_edit_field_check;
ALTER TABLE operation_drafts ADD CHECK(finance_edit_field IN ('amount','date','description','account','destination','reason','received'));
