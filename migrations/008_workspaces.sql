SET search_path=balans,pg_catalog;
ALTER TABLE workspaces DROP CONSTRAINT workspaces_kind_check;
ALTER TABLE workspaces ADD CHECK(kind IN ('personal','shared'));
ALTER TABLE workspaces DROP CONSTRAINT workspaces_owner_user_id_kind_key;
CREATE UNIQUE INDEX one_personal_workspace ON workspaces(owner_user_id) WHERE kind='personal';
ALTER TABLE workspaces ADD CONSTRAINT workspace_name_length CHECK(length(name) BETWEEN 1 AND 80);
ALTER TABLE memberships DROP CONSTRAINT memberships_role_check;
ALTER TABLE memberships ADD CHECK(role IN ('owner','participant'));
ALTER TABLE memberships DROP CONSTRAINT memberships_status_check;
ALTER TABLE memberships ADD CHECK(status IN ('active','pending','removed'));
ALTER TABLE memberships ADD COLUMN owner_user_id uuid REFERENCES users,ADD COLUMN telegram_user_id bigint;
-- Transactional backfill by the schema owner; FORCE RLS is restored before commit.
ALTER TABLE memberships NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;
ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
UPDATE memberships m SET owner_user_id=w.owner_user_id,telegram_user_id=u.telegram_user_id FROM workspaces w,users u WHERE w.id=m.workspace_id AND u.id=m.user_id;
ALTER TABLE memberships ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE memberships ALTER COLUMN telegram_user_id SET NOT NULL;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces ADD COLUMN timezone text NOT NULL DEFAULT 'Europe/Moscow';
ALTER TABLE user_settings ADD COLUMN selected_workspace_id uuid REFERENCES workspaces;
DROP POLICY own ON memberships;
CREATE POLICY own ON memberships USING(user_id=actor_user_id() OR owner_user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id() OR owner_user_id=actor_user_id());
DROP POLICY own ON workspaces;
CREATE POLICY own ON workspaces USING(owner_user_id=actor_user_id() OR id IN(SELECT workspace_id FROM memberships WHERE user_id=actor_user_id() AND status='active')) WITH CHECK(owner_user_id=actor_user_id());
CREATE FUNCTION current_workspace() RETURNS uuid LANGUAGE sql STABLE SET search_path=balans,pg_temp AS $$
 SELECT coalesce((SELECT w.id FROM user_settings s JOIN workspaces w ON w.id=s.selected_workspace_id WHERE s.user_id=actor_user_id()),(SELECT id FROM workspaces WHERE owner_user_id=actor_user_id() AND kind='personal'))
$$;
CREATE FUNCTION owns_workspace(w uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path=balans,pg_temp AS $$ SELECT EXISTS(SELECT 1 FROM workspaces WHERE id=w AND owner_user_id=actor_user_id()) $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['accounts','categories','operations','operation_revisions','journal_entries','postings','operation_drafts','audit_log'] LOOP EXECUTE format('DROP POLICY own ON %I',t); END LOOP;
END $$;
CREATE POLICY own ON accounts USING(workspace_id=current_workspace() AND (owns_workspace(workspace_id) OR responsible_membership_id IN(SELECT id FROM memberships WHERE user_id=actor_user_id() AND status='active'))) WITH CHECK(workspace_id=current_workspace() AND owns_workspace(workspace_id));
CREATE POLICY own ON categories USING(workspace_id=current_workspace()) WITH CHECK(workspace_id=current_workspace() AND owns_workspace(workspace_id));
CREATE POLICY own ON operations USING(workspace_id=current_workspace() AND (created_by_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND created_by_user_id=actor_user_id());
CREATE POLICY own ON operation_revisions USING(workspace_id=current_workspace() AND operation_id IN(SELECT id FROM operations)) WITH CHECK(workspace_id=current_workspace() AND operation_id IN(SELECT id FROM operations WHERE created_by_user_id=actor_user_id()));
CREATE POLICY own ON journal_entries USING(workspace_id=current_workspace() AND revision_id IN(SELECT id FROM operation_revisions)) WITH CHECK(workspace_id=current_workspace() AND revision_id IN(SELECT id FROM operation_revisions));
CREATE POLICY own ON postings USING(workspace_id=current_workspace() AND account_id IN(SELECT id FROM accounts)) WITH CHECK(workspace_id=current_workspace() AND account_id IN(SELECT id FROM accounts));
CREATE POLICY own ON operation_drafts USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR (state='saved' AND owns_workspace(workspace_id)))) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE POLICY own ON audit_log USING(workspace_id=current_workspace() AND operation_id IN(SELECT id FROM operations)) WITH CHECK(workspace_id=current_workspace() AND actor_user_id=actor_user_id());
-- Private jobs/rules remain private across all currently accessible workspaces. File access additionally allows the owner of a shared workspace.
DROP POLICY own ON receipt_files;
CREATE POLICY own ON receipt_files USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
DROP POLICY own ON receipt_items;
CREATE POLICY own ON receipt_items USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE TABLE workspace_invitations(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,owner_user_id uuid NOT NULL REFERENCES users,owner_telegram_id bigint NOT NULL,workspace_name text NOT NULL,token uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),state text NOT NULL DEFAULT 'open' CHECK(state IN ('open','pending','accepted','rejected','revoked')),claimant_user_id uuid REFERENCES users,claimant_telegram_id bigint,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours');
ALTER TABLE workspace_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON workspace_invitations USING(owner_user_id=actor_user_id() OR claimant_user_id=actor_user_id() OR token::text=current_setting('balans.invitation_token',true)) WITH CHECK(owner_user_id=actor_user_id() OR claimant_user_id=actor_user_id() OR token::text=current_setting('balans.invitation_token',true));
CREATE FUNCTION create_workspace(workspace_name text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w uuid; m uuid; u uuid:=actor_user_id();
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 IF length(trim(workspace_name)) NOT BETWEEN 1 AND 80 THEN RAISE EXCEPTION 'Название: 1–80 символов'; END IF;
 IF (SELECT count(*) FROM workspaces WHERE owner_user_id=u AND kind='shared')>=10 THEN RAISE EXCEPTION 'Лимит: 10 совместных бюджетов'; END IF;
 INSERT INTO workspaces(owner_user_id,kind,name,timezone) VALUES(u,'shared',trim(workspace_name),(SELECT timezone FROM user_settings WHERE user_id=u)) RETURNING id INTO w;
 INSERT INTO memberships(workspace_id,user_id,role,owner_user_id,telegram_user_id) VALUES(w,u,'owner',u,actor_telegram_id()) RETURNING id INTO m;
 UPDATE user_settings SET selected_workspace_id=w,default_account_id=NULL WHERE user_id=u;
 INSERT INTO accounts(workspace_id,responsible_membership_id,name) VALUES(w,m,'Средства руководителя');
 INSERT INTO categories(workspace_id,name) SELECT w,unnest(ARRAY['Продукты','Кафе и рестораны','Транспорт','Дом','Здоровье','Покупки','Развлечения','Другое']);
 RETURN w;
END $$;
CREATE FUNCTION new_invitation() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces; token_id uuid;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=current_workspace() AND kind='shared' AND owner_user_id=actor_user_id();
 IF NOT FOUND THEN RAISE EXCEPTION 'Выберите совместный бюджет, которым руководите'; END IF;
 INSERT INTO workspace_invitations(workspace_id,owner_user_id,owner_telegram_id,workspace_name) VALUES(w.id,w.owner_user_id,actor_telegram_id(),w.name) RETURNING token INTO token_id;
 RETURN token_id;
END $$;
CREATE FUNCTION join_workspace(token_id uuid,accept boolean) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE inv workspace_invitations; m memberships;
BEGIN
 PERFORM set_config('balans.invitation_token',token_id::text,true);
 SELECT * INTO inv FROM workspace_invitations WHERE token=token_id FOR UPDATE;
 IF NOT FOUND OR inv.expires_at<=now() OR inv.state NOT IN ('open','pending') OR (inv.claimant_user_id IS NOT NULL AND inv.claimant_user_id<>actor_user_id()) THEN RAISE EXCEPTION 'Приглашение недоступно, использовано или истекло'; END IF;
 IF inv.owner_user_id=actor_user_id() THEN RAISE EXCEPTION 'Вы уже руководитель этого бюджета'; END IF;
 IF accept THEN
  SELECT * INTO m FROM memberships WHERE workspace_id=inv.workspace_id AND user_id=actor_user_id();
  IF FOUND AND m.status='active' THEN RAISE EXCEPTION 'Вы уже участник'; END IF;
  UPDATE workspace_invitations SET claimant_user_id=actor_user_id(),claimant_telegram_id=actor_telegram_id(),state='pending' WHERE id=inv.id;
  INSERT INTO memberships(workspace_id,user_id,role,status,owner_user_id,telegram_user_id) VALUES(inv.workspace_id,actor_user_id(),'participant','pending',inv.owner_user_id,actor_telegram_id()) ON CONFLICT(workspace_id,user_id) DO UPDATE SET status='pending';
 END IF;
 RETURN jsonb_build_object('name',inv.workspace_name,'owner',inv.owner_telegram_id,'accepted',accept);
END $$;
CREATE FUNCTION decide_invitation(invitation_id uuid,accept boolean) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE inv workspace_invitations; m uuid;
BEGIN
 SELECT * INTO inv FROM workspace_invitations WHERE id=invitation_id AND owner_user_id=actor_user_id() AND workspace_id=current_workspace() FOR UPDATE;
 IF NOT FOUND OR inv.state<>'pending' OR inv.expires_at<=now() THEN RAISE EXCEPTION 'Заявка недоступна или истекла'; END IF;
 UPDATE workspace_invitations SET state=CASE WHEN accept THEN 'accepted' ELSE 'rejected' END WHERE id=inv.id;
 UPDATE memberships SET status=CASE WHEN accept THEN 'active' ELSE 'removed' END WHERE workspace_id=inv.workspace_id AND user_id=inv.claimant_user_id RETURNING id INTO m;
 IF accept AND NOT EXISTS(SELECT 1 FROM accounts WHERE responsible_membership_id=m) THEN INSERT INTO accounts(workspace_id,responsible_membership_id,name) VALUES(inv.workspace_id,m,'Участник '||inv.claimant_telegram_id::text); END IF;
END $$;
CREATE FUNCTION remove_membership(member_id uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE m memberships;
BEGIN
 SELECT * INTO m FROM memberships WHERE id=member_id AND workspace_id=current_workspace() AND role='participant' AND (owner_user_id=actor_user_id() OR user_id=actor_user_id()) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Участник недоступен'; END IF;
 UPDATE memberships SET status='removed' WHERE id=m.id;
 UPDATE workspace_invitations SET state='revoked' WHERE workspace_id=m.workspace_id AND claimant_user_id=m.user_id AND state IN ('open','pending');
END $$;
REVOKE ALL ON FUNCTION current_workspace(),owns_workspace(uuid),create_workspace(text),new_invitation(),join_workspace(uuid,boolean),decide_invitation(uuid,boolean),remove_membership(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION bootstrap() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = balans, pg_temp AS $$
DECLARE u uuid; w uuid; m uuid;
BEGIN
  INSERT INTO users(telegram_user_id) VALUES(actor_telegram_id()) ON CONFLICT DO NOTHING;
  u := actor_user_id();
  IF u IS NULL THEN RAISE EXCEPTION 'Access denied'; END IF;
  IF EXISTS(SELECT 1 FROM workspaces WHERE owner_user_id=u) THEN RETURN u; END IF;
  INSERT INTO user_settings(user_id) VALUES(u);
  INSERT INTO workspaces(owner_user_id) VALUES(u) RETURNING id INTO w;
  INSERT INTO memberships(workspace_id,user_id,owner_user_id,telegram_user_id) VALUES(w,u,u,actor_telegram_id()) RETURNING id INTO m;
  INSERT INTO accounts(workspace_id,responsible_membership_id) VALUES(w,m);
  INSERT INTO categories(workspace_id,name) SELECT w,unnest(ARRAY['Продукты','Кафе и рестораны','Транспорт','Дом','Здоровье','Покупки','Развлечения','Другое']);
  RETURN u;
END $$;


CREATE OR REPLACE FUNCTION create_account(account_name text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE a uuid; w uuid; m uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 IF length(trim(account_name)) NOT BETWEEN 1 AND 80 THEN RAISE EXCEPTION 'Название счёта: 1–80 символов'; END IF;
 SELECT id INTO w FROM workspaces WHERE owner_user_id=actor_user_id() AND id=current_workspace();
 IF w IS NULL THEN RAISE EXCEPTION 'Бюджет недоступен'; END IF;
 SELECT id INTO m FROM memberships WHERE workspace_id=w AND user_id=actor_user_id();
 SELECT id INTO a FROM accounts WHERE workspace_id=w AND lower(name)=lower(trim(account_name));
 IF a IS NOT NULL THEN RETURN a; END IF;
 INSERT INTO accounts(workspace_id,responsible_membership_id,name) VALUES(w,m,trim(account_name)) RETURNING id INTO a;
 RETURN a;
END $$;

CREATE OR REPLACE FUNCTION manage_category(action_name text,category_name text,new_name text DEFAULT NULL) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w uuid; cat categories; result uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 SELECT id INTO w FROM workspaces WHERE owner_user_id=actor_user_id() AND id=current_workspace();
 IF w IS NULL THEN RAISE EXCEPTION 'Бюджет недоступен'; END IF;
 IF length(trim(category_name)) NOT BETWEEN 1 AND 60 OR (new_name IS NOT NULL AND length(trim(new_name)) NOT BETWEEN 1 AND 60) THEN RAISE EXCEPTION 'Название: 1–60 символов'; END IF;
 SELECT * INTO cat FROM categories WHERE workspace_id=w AND lower(name)=lower(trim(category_name));
 IF action_name='add' THEN
  IF cat.id IS NOT NULL THEN RETURN cat.id; END IF;
  IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)>=30 THEN RAISE EXCEPTION 'Лимит: 30 активных категорий'; END IF;
  INSERT INTO categories(workspace_id,name) VALUES(w,trim(category_name)) RETURNING id INTO result;
 ELSIF action_name IN ('rename','archive','restore') THEN
  IF cat.id IS NULL THEN RAISE EXCEPTION 'Категория не найдена'; END IF;
  result:=cat.id;
  IF action_name='rename' THEN
   IF new_name IS NULL OR EXISTS(SELECT 1 FROM categories WHERE workspace_id=w AND lower(name)=lower(trim(new_name)) AND id<>cat.id) THEN RAISE EXCEPTION 'Новое название отсутствует или уже занято'; END IF;
   UPDATE categories SET name=trim(new_name) WHERE id=cat.id;
  ELSIF action_name='archive' THEN
   IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)<=1 THEN RAISE EXCEPTION 'Оставьте хотя бы одну активную категорию'; END IF;
   UPDATE categories SET archived=true WHERE id=cat.id;
   UPDATE category_rules SET enabled=false WHERE category_id=cat.id;
  ELSE
   IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)>=30 AND cat.archived THEN RAISE EXCEPTION 'Лимит: 30 активных категорий'; END IF;
   UPDATE categories SET archived=false WHERE id=cat.id;
  END IF;
 ELSE RAISE EXCEPTION 'Неизвестное действие'; END IF;
 INSERT INTO category_events(workspace_id,author_user_id,category_id,action,old_name,new_name) VALUES(w,actor_user_id(),result,action_name,cat.name,coalesce(new_name,category_name));
 RETURN result;
END $$;
DROP POLICY own ON receipt_batches;
CREATE POLICY own ON receipt_batches USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR (state='ready' AND owns_workspace(workspace_id)))) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());

-- Durable, immutable audit of access changes, including voluntary departure.
CREATE TABLE workspace_events(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,owner_user_id uuid NOT NULL,actor_user_id uuid NOT NULL,subject_user_id uuid,event_type text NOT NULL,old_status text,new_status text,created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE workspace_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_events FORCE ROW LEVEL SECURITY;
CREATE POLICY access ON workspace_events USING(owner_user_id=actor_user_id() OR subject_user_id=actor_user_id()) WITH CHECK(actor_user_id=actor_user_id());
CREATE FUNCTION log_membership_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 IF TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
 INSERT INTO workspace_events(workspace_id,owner_user_id,actor_user_id,subject_user_id,event_type,old_status,new_status) VALUES(NEW.workspace_id,NEW.owner_user_id,actor_user_id(),NEW.user_id,'membership',CASE WHEN TG_OP='UPDATE' THEN OLD.status END,NEW.status);
 END IF; RETURN NEW;
END $$;
CREATE TRIGGER membership_audit AFTER INSERT OR UPDATE ON memberships FOR EACH ROW EXECUTE FUNCTION log_membership_change();
CREATE FUNCTION revoke_invitation(invitation_id uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE inv workspace_invitations;
BEGIN
 SELECT * INTO inv FROM workspace_invitations WHERE id=invitation_id AND owner_user_id=actor_user_id() AND workspace_id=current_workspace() FOR UPDATE;
 IF NOT FOUND OR inv.state NOT IN ('open','pending') THEN RAISE EXCEPTION 'Приглашение недоступно или уже закрыто'; END IF;
 UPDATE workspace_invitations SET state='revoked' WHERE id=inv.id;
 UPDATE memberships SET status='removed' WHERE workspace_id=inv.workspace_id AND user_id=inv.claimant_user_id AND status='pending';
 INSERT INTO workspace_events(workspace_id,owner_user_id,actor_user_id,subject_user_id,event_type,old_status,new_status) VALUES(inv.workspace_id,inv.owner_user_id,actor_user_id(),inv.claimant_user_id,'invitation',inv.state,'revoked');
END $$;
REVOKE ALL ON FUNCTION log_membership_change(),revoke_invitation(uuid) FROM PUBLIC;

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
