SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies,ADD COLUMN currency_selected_at timestamptz;
-- Existing histories keep their currency; nothing is relabelled or converted.
ALTER TABLE user_settings NO FORCE ROW LEVEL SECURITY;
ALTER TABLE accounts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;
UPDATE user_settings s SET currency=coalesce((SELECT a.currency FROM accounts a JOIN workspaces w ON w.id=a.workspace_id WHERE a.id=s.default_account_id AND w.kind='personal'),(SELECT w.base_currency FROM workspaces w WHERE w.owner_user_id=s.user_id AND w.kind='personal'),'RUB'),currency_selected_at=now();
ALTER TABLE user_settings FORCE ROW LEVEL SECURITY;
ALTER TABLE accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE FUNCTION choose_user_currency(code text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE person uuid:=actor_user_id();previous text;space uuid;t text;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM currencies WHERE currencies.code=choose_user_currency.code) THEN RAISE EXCEPTION 'Валюта недоступна';END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(person::text,24001));
 SELECT currency INTO previous FROM user_settings WHERE user_id=person FOR UPDATE;
 IF previous=code THEN UPDATE user_settings SET currency_selected_at=now() WHERE user_id=person;RETURN;END IF;
 FOREACH t IN ARRAY ARRAY['workspaces','accounts','operations','operation_drafts','receipt_batches','voice_jobs','memberships'] LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 SELECT id INTO space FROM workspaces WHERE owner_user_id=person AND kind='personal';
 IF EXISTS(SELECT 1 FROM operations WHERE workspace_id=space) THEN RAISE EXCEPTION 'Валюта уже используется в сохранённых операциях. Смена требует отдельного пересчёта истории; суммы не изменены.';END IF;
 IF EXISTS(SELECT 1 FROM operation_drafts WHERE author_user_id=person AND state='pending') OR EXISTS(SELECT 1 FROM receipt_batches WHERE author_user_id=person AND state IN ('collecting','processing')) OR EXISTS(SELECT 1 FROM voice_jobs WHERE author_user_id=person AND state='processing') THEN RAISE EXCEPTION 'Сначала завершите текущую запись или отмените ввод.';END IF;
 UPDATE user_settings SET currency=code,currency_selected_at=now() WHERE user_id=person;
 UPDATE workspaces SET base_currency=code WHERE id=space;
 UPDATE accounts SET currency=code WHERE workspace_id=space;
 FOREACH t IN ARRAY ARRAY['workspaces','accounts','operations','operation_drafts','receipt_batches','voice_jobs','memberships'] LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
END $$;
CREATE FUNCTION new_workspace_currency() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN NEW.base_currency:=coalesce((SELECT currency FROM user_settings WHERE user_id=NEW.owner_user_id),NEW.base_currency);RETURN NEW;END $$;
CREATE TRIGGER workspace_currency BEFORE INSERT ON workspaces FOR EACH ROW EXECUTE FUNCTION new_workspace_currency();
CREATE OR REPLACE FUNCTION default_shared_account_currency() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces;chosen text;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=NEW.workspace_id;
 IF w.kind='shared' THEN NEW.currency:=w.base_currency;
 ELSE
  SELECT s.currency INTO chosen FROM user_settings s WHERE s.user_id=w.owner_user_id AND s.currency_selected_at IS NOT NULL;
  IF chosen IS NOT NULL THEN NEW.currency:=chosen;END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION choose_user_currency(text),new_workspace_currency() FROM PUBLIC;
