SET search_path=balans,pg_catalog;
CREATE TABLE fund_transfers(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,
 sender_user_id uuid NOT NULL REFERENCES users,recipient_user_id uuid NOT NULL REFERENCES users,
 sender_telegram_id bigint NOT NULL,recipient_telegram_id bigint NOT NULL,
 source_account_id uuid NOT NULL,target_account_id uuid,
 amount numeric(20,2) NOT NULL CHECK(amount>0 AND amount<=999999999999.99),received numeric(20,2) NOT NULL DEFAULT 0 CHECK(received>=0 AND received<=amount),
 purpose text NOT NULL CHECK(length(purpose) BETWEEN 1 AND 500),occurred_on date NOT NULL,due_on date,
 state text NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','sent','received','cancelled')),
 dispute text,version integer NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,source_account_id) REFERENCES accounts(workspace_id,id),
 FOREIGN KEY(workspace_id,target_account_id) REFERENCES accounts(workspace_id,id),
 CHECK(sender_user_id<>recipient_user_id)
);
CREATE TABLE fund_claims(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,
 author_user_id uuid NOT NULL REFERENCES users,author_telegram_id bigint NOT NULL,account_id uuid NOT NULL,
 amount numeric(20,2) NOT NULL CHECK(amount>0 AND amount<=999999999999.99),source text NOT NULL CHECK(length(source) BETWEEN 1 AND 200),purpose text NOT NULL CHECK(length(purpose) BETWEEN 1 AND 500),occurred_on date NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','matched','external','rejected')),
 transfer_id uuid REFERENCES fund_transfers,reason text,created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id)
);
CREATE TABLE fund_entries(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,
 transfer_id uuid REFERENCES fund_transfers,claim_id uuid REFERENCES fund_claims,
 actor_user_id uuid NOT NULL REFERENCES users,account_id uuid,delta numeric(20,2) NOT NULL CHECK(delta<>0),
 effective_on date NOT NULL,event_type text NOT NULL CHECK(event_type IN ('sent','received','external')),
 event_key text NOT NULL,line integer NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(event_key,line),FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id),
 CHECK((transfer_id IS NULL)<>(claim_id IS NULL))
);
CREATE TABLE fund_audit(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,actor_user_id uuid NOT NULL REFERENCES users,transfer_id uuid REFERENCES fund_transfers,claim_id uuid REFERENCES fund_claims,action text NOT NULL,reason text,created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE fund_transfers ENABLE ROW LEVEL SECURITY; ALTER TABLE fund_transfers FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON fund_transfers USING(workspace_id=current_workspace() AND (sender_user_id=actor_user_id() OR recipient_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND (sender_user_id=actor_user_id() OR recipient_user_id=actor_user_id() OR owns_workspace(workspace_id)));
ALTER TABLE fund_claims ENABLE ROW LEVEL SECURITY; ALTER TABLE fund_claims FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON fund_claims USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id)));
ALTER TABLE fund_entries ENABLE ROW LEVEL SECURITY; ALTER TABLE fund_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON fund_entries USING(workspace_id=current_workspace() AND (owns_workspace(workspace_id) OR account_id IN(SELECT id FROM accounts))) WITH CHECK(workspace_id=current_workspace() AND actor_user_id=actor_user_id());
ALTER TABLE fund_audit ENABLE ROW LEVEL SECURITY; ALTER TABLE fund_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON fund_audit USING(workspace_id=current_workspace() AND (transfer_id IN(SELECT id FROM fund_transfers) OR claim_id IN(SELECT id FROM fund_claims))) WITH CHECK(workspace_id=current_workspace() AND actor_user_id=actor_user_id());
CREATE FUNCTION create_fund_transfer(recipient_telegram bigint,source_account uuid,value numeric,day date,note text,due date DEFAULT NULL) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces; recipient uuid; rid bigint; result uuid;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=current_workspace() AND kind='shared';
 IF NOT FOUND THEN RAISE EXCEPTION 'Выберите совместный бюджет'; END IF;
 IF NOT EXISTS(SELECT 1 FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE a.id=source_account AND m.user_id=actor_user_id() AND m.status='active') THEN RAISE EXCEPTION 'Счёт недоступен'; END IF;
 IF w.owner_user_id=actor_user_id() THEN
  SELECT user_id,telegram_user_id INTO recipient,rid FROM memberships WHERE workspace_id=w.id AND telegram_user_id=recipient_telegram AND status='active' AND role='participant';
  IF NOT FOUND THEN RAISE EXCEPTION 'Участник недоступен'; END IF;
 ELSE
  recipient:=w.owner_user_id;
  SELECT owner_telegram_id INTO rid FROM workspace_invitations WHERE workspace_id=w.id AND claimant_user_id=actor_user_id() AND state='accepted' ORDER BY created_at DESC LIMIT 1;
 END IF;
 IF value<>round(value,2) OR value<=0 OR value>999999999999.99 OR day>(now() AT TIME ZONE w.timezone)::date OR (due IS NOT NULL AND due<day) OR length(trim(note)) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Проверьте сумму, дату, назначение и срок отчёта'; END IF;
 INSERT INTO fund_transfers(workspace_id,sender_user_id,recipient_user_id,sender_telegram_id,recipient_telegram_id,source_account_id,amount,occurred_on,purpose,due_on) VALUES(w.id,actor_user_id(),recipient,actor_telegram_id(),rid,source_account,value,day,trim(note),due) RETURNING id INTO result;
 INSERT INTO fund_audit(workspace_id,actor_user_id,transfer_id,action) VALUES(w.id,actor_user_id(),result,'planned'); RETURN result;
END $$;
CREATE FUNCTION fund_action(identity uuid,action_name text,expected integer,value numeric DEFAULT NULL,account uuid DEFAULT NULL,note text DEFAULT NULL) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE f fund_transfers; key text; day date;
BEGIN
 SELECT * INTO f FROM fund_transfers WHERE id=identity FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Передача недоступна'; END IF;
 IF f.version<>expected THEN RAISE EXCEPTION 'Карточка устарела; откройте /funds. Повторной записи нет'; END IF;
 key:=f.id::text||':'||f.version::text;
 day:=(now() AT TIME ZONE (SELECT timezone FROM workspaces WHERE id=f.workspace_id))::date;
 IF action_name IN ('send','cancel') THEN
  IF f.sender_user_id<>actor_user_id() OR f.state<>'planned' THEN RAISE EXCEPTION 'Действие недоступно'; END IF;
  IF NOT EXISTS(SELECT 1 FROM workspaces WHERE id=f.workspace_id AND owner_user_id=f.recipient_user_id) AND NOT EXISTS(SELECT 1 FROM memberships WHERE workspace_id=f.workspace_id AND user_id=f.recipient_user_id AND status='active') THEN RAISE EXCEPTION 'Получатель больше не участвует в бюджете'; END IF;
  UPDATE fund_transfers SET state=CASE WHEN action_name='send' THEN 'sent' ELSE 'cancelled' END,version=version+1 WHERE id=f.id;
  IF action_name='send' THEN
   INSERT INTO fund_entries(workspace_id,transfer_id,actor_user_id,account_id,delta,effective_on,event_type,event_key,line) VALUES(f.workspace_id,f.id,actor_user_id(),f.source_account_id,-f.amount,f.occurred_on,'sent',key,1),(f.workspace_id,f.id,actor_user_id(),NULL,f.amount,f.occurred_on,'sent',key,2);
  END IF;
 ELSIF action_name IN ('receive','dispute') THEN
  IF f.recipient_user_id<>actor_user_id() OR f.state<>'sent' THEN RAISE EXCEPTION 'Подтверждение доступно только получателю отправленной передачи'; END IF;
  IF action_name='dispute' THEN
   IF coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Укажите причину расхождения'; END IF;
   UPDATE fund_transfers SET dispute=note,version=version+1 WHERE id=f.id;
  ELSE
   IF value IS NULL OR value<>round(value,2) OR value<=0 OR value>f.amount-f.received THEN RAISE EXCEPTION 'Полученная сумма должна быть больше нуля и не превышать остаток в пути'; END IF;
   IF NOT EXISTS(SELECT 1 FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE a.id=account AND m.user_id=actor_user_id() AND m.status='active') OR (f.target_account_id IS NOT NULL AND f.target_account_id<>account) THEN RAISE EXCEPTION 'Выберите прежний счёт получения'; END IF;
   UPDATE fund_transfers SET received=received+value,target_account_id=account,state=CASE WHEN received+value=amount THEN 'received' ELSE 'sent' END,version=version+1 WHERE id=f.id;
   INSERT INTO fund_entries(workspace_id,transfer_id,actor_user_id,account_id,delta,effective_on,event_type,event_key,line) VALUES(f.workspace_id,f.id,actor_user_id(),NULL,-value,day,'received',key,1),(f.workspace_id,f.id,actor_user_id(),account,value,day,'received',key,2);
  END IF;
 ELSE RAISE EXCEPTION 'Неизвестное действие'; END IF;
 INSERT INTO fund_audit(workspace_id,actor_user_id,transfer_id,action,reason) VALUES(f.workspace_id,actor_user_id(),f.id,action_name,note);
END $$;
CREATE FUNCTION create_fund_claim(account uuid,value numeric,day date,origin text,note text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces; result uuid;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=current_workspace() AND kind='shared';
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE a.id=account AND m.user_id=actor_user_id() AND m.status='active') THEN RAISE EXCEPTION 'Выберите свой счёт совместного бюджета'; END IF;
 IF value<>round(value,2) OR value<=0 OR value>999999999999.99 OR day>(now() AT TIME ZONE w.timezone)::date THEN RAISE EXCEPTION 'Проверьте сумму и дату'; END IF;
 INSERT INTO fund_claims(workspace_id,author_user_id,author_telegram_id,account_id,amount,occurred_on,source,purpose) VALUES(w.id,actor_user_id(),actor_telegram_id(),account,value,day,origin,note) RETURNING id INTO result;
 INSERT INTO fund_audit(workspace_id,actor_user_id,claim_id,action) VALUES(w.id,actor_user_id(),result,'claimed');RETURN result;
END $$;
CREATE FUNCTION reconcile_claim(identity uuid,action_name text,note text,transfer uuid DEFAULT NULL) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE cl fund_claims; f fund_transfers; key text;
BEGIN
 SELECT * INTO cl FROM fund_claims WHERE id=identity FOR UPDATE;
 IF NOT FOUND OR NOT owns_workspace(cl.workspace_id) OR cl.state<>'pending' THEN RAISE EXCEPTION 'Заявленный приход недоступен или уже сверен'; END IF;
 IF coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Укажите основание сверки'; END IF;
 IF action_name='external' THEN
  INSERT INTO fund_entries(workspace_id,claim_id,actor_user_id,account_id,delta,effective_on,event_type,event_key,line) VALUES(cl.workspace_id,cl.id,actor_user_id(),cl.account_id,cl.amount,cl.occurred_on,'external',cl.id::text,1);
 ELSIF action_name='matched' THEN
  SELECT * INTO f FROM fund_transfers WHERE id=transfer AND workspace_id=cl.workspace_id AND recipient_user_id=cl.author_user_id FOR UPDATE;
  IF NOT FOUND OR cl.occurred_on<f.occurred_on OR f.state<>'sent' OR cl.amount>f.amount-f.received OR (f.target_account_id IS NOT NULL AND f.target_account_id<>cl.account_id) THEN RAISE EXCEPTION 'Нет подходящей несверенной выдачи на эту сумму'; END IF;
  key:=f.id::text||':'||f.version::text;
  INSERT INTO fund_entries(workspace_id,transfer_id,actor_user_id,account_id,delta,effective_on,event_type,event_key,line) VALUES(f.workspace_id,f.id,actor_user_id(),NULL,-cl.amount,cl.occurred_on,'received',key,1),(f.workspace_id,f.id,actor_user_id(),cl.account_id,cl.amount,cl.occurred_on,'received',key,2);
  UPDATE fund_transfers SET received=received+cl.amount,target_account_id=cl.account_id,state=CASE WHEN received+cl.amount=amount THEN 'received' ELSE 'sent' END,version=version+1 WHERE id=f.id;
 ELSIF action_name<>'rejected' THEN RAISE EXCEPTION 'Неизвестный результат сверки'; END IF;
 UPDATE fund_claims SET state=action_name,reason=note,transfer_id=transfer WHERE id=cl.id;
 INSERT INTO fund_audit(workspace_id,actor_user_id,claim_id,transfer_id,action,reason) VALUES(cl.workspace_id,actor_user_id(),cl.id,transfer,action_name,note);
END $$;
REVOKE ALL ON FUNCTION create_fund_transfer(bigint,uuid,numeric,date,text,date),fund_action(uuid,text,integer,numeric,uuid,text),create_fund_claim(uuid,numeric,date,text,text),reconcile_claim(uuid,text,text,uuid) FROM PUBLIC;
CREATE TABLE fund_confirmations(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,payload jsonb NOT NULL,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','cancelled')),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours');
ALTER TABLE fund_confirmations ENABLE ROW LEVEL SECURITY; ALTER TABLE fund_confirmations FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON fund_confirmations USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());

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
