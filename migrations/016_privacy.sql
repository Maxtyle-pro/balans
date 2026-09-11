SET search_path=balans,pg_catalog;
ALTER TABLE workspaces ADD COLUMN archived boolean NOT NULL DEFAULT false;
ALTER TABLE user_settings ADD COLUMN keep_personal_originals boolean NOT NULL DEFAULT true,ADD COLUMN service_consent_version integer,ADD COLUMN service_consented_at timestamptz;
CREATE TABLE privacy_policy(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),required boolean NOT NULL DEFAULT false,version integer NOT NULL DEFAULT 1,terms_url text NOT NULL DEFAULT '',privacy_url text NOT NULL DEFAULT '',operator_country text NOT NULL DEFAULT '',storage_country text NOT NULL DEFAULT '',payment_retention text NOT NULL DEFAULT 'Не утверждено оператором');
INSERT INTO privacy_policy DEFAULT VALUES;
CREATE TABLE erasure_requests(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,telegram_hash text NOT NULL,telegram_user_id bigint,anonymous_telegram_id bigint NOT NULL DEFAULT (8000000000000000000+floor(random()*100000000000000000)::bigint),state text NOT NULL DEFAULT 'preview' CHECK(state IN ('preview','queued','files_pending','completed')),confirmed_at timestamptz,completed_at timestamptz,files jsonb NOT NULL DEFAULT '[]',created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '1 hour',UNIQUE(telegram_hash));
ALTER TABLE erasure_requests ENABLE ROW LEVEL SECURITY;ALTER TABLE erasure_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON erasure_requests USING(telegram_hash=current_setting('balans.actor_hash',true) OR current_setting('balans.erasure_worker',true)='on') WITH CHECK(telegram_hash=current_setting('balans.actor_hash',true) OR current_setting('balans.erasure_worker',true)='on');
CREATE FUNCTION confirm_erasure(identity uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE request erasure_requests;
BEGIN
 SELECT * INTO request FROM erasure_requests WHERE id=identity AND user_id=actor_user_id() AND state='preview' AND expires_at>now() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Подтверждение удаления истекло.';END IF;
 UPDATE erasure_requests SET state='queued',confirmed_at=now() WHERE id=identity;
 UPDATE notification_preferences SET enabled=false,blocked=true WHERE user_id=request.user_id;
 UPDATE notification_outbox SET state='cancelled' WHERE user_id=request.user_id AND state IN ('pending','sending');
 UPDATE user_settings SET ai_enabled=false,voice_enabled=false,receipts_enabled=false WHERE user_id=request.user_id;
 UPDATE diagnostic_grants SET revoked=true,payload='{}' WHERE user_id=request.user_id;
 UPDATE workspaces SET archived=true WHERE owner_user_id=request.user_id AND kind='shared';
 UPDATE memberships SET status='removed' WHERE user_id=request.user_id AND role='participant' AND status<>'removed';
 UPDATE users SET status='suspended' WHERE id=request.user_id;
END $$;
REVOKE ALL ON FUNCTION confirm_erasure(uuid) FROM PUBLIC;
-- Workspace-owned records can be physically erased together. Shared workspaces are never
-- deleted by account erasure, so other participants' history is retained.
DO $$ DECLARE r record;definition text;BEGIN
 FOR r IN SELECT conname,conrelid::regclass AS relation,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE contype='f' AND confrelid='workspaces'::regclass LOOP
  definition:=r.definition;
  IF definition NOT LIKE '%ON DELETE%' THEN definition:=regexp_replace(definition,'( DEFERRABLE.*)?$',' ON DELETE CASCADE\1');END IF;
  EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',r.relation,r.conname);
  EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s',r.relation,r.conname,definition);
 END LOOP;
END $$;
CREATE FUNCTION erase_account(identity uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE request erasure_requests;forced text[];t text;manifest jsonb;old_mode text:=current_setting('balans.erasure_worker',true);
BEGIN
 PERFORM pg_advisory_xact_lock(16001001);PERFORM set_config('balans.erasure_worker','on',true);
 SELECT * INTO request FROM erasure_requests WHERE id=identity AND state IN ('queued','files_pending') FOR UPDATE;
 IF NOT FOUND THEN PERFORM set_config('balans.erasure_worker',coalesce(old_mode,''),true);RETURN '[]';END IF;
 IF request.state='files_pending' THEN PERFORM set_config('balans.erasure_worker',coalesce(old_mode,''),true);RETURN request.files;END IF;
 SELECT array_agg(relname) INTO forced FROM pg_class WHERE relnamespace='balans'::regnamespace AND relkind='r' AND relforcerowsecurity;
 FOREACH t IN ARRAY forced LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 SELECT coalesce(jsonb_agg(file),'[]') INTO manifest FROM (
  SELECT jsonb_build_object('kind','receipt','id',f.id) AS file FROM receipt_files f WHERE author_user_id=request.user_id
  UNION ALL SELECT jsonb_build_object('kind','personal','id',d.id) FROM documents d JOIN workspaces w ON w.id=d.workspace_id WHERE w.kind='personal' AND w.owner_user_id=request.user_id
 ) files;
 UPDATE erasure_requests SET files=manifest WHERE id=identity;
 -- Unlink personal settings before cascading the personal workspace.
 DELETE FROM user_settings WHERE user_id=request.user_id;
 DELETE FROM workspaces WHERE owner_user_id=request.user_id AND kind='personal';
 DELETE FROM telegram_updates WHERE user_id=request.user_id;
 DELETE FROM share_drafts WHERE author_user_id=request.user_id;
 DELETE FROM support_tickets WHERE user_id=request.user_id;
 DELETE FROM diagnostic_grants WHERE user_id=request.user_id;
 DELETE FROM admin_sessions WHERE telegram_user_id=request.telegram_user_id;
 DELETE FROM admin_login_codes WHERE telegram_user_id=request.telegram_user_id;
 UPDATE admin_roles SET active=false WHERE telegram_user_id=request.telegram_user_id;
 DELETE FROM notification_outbox WHERE user_id=request.user_id;
 DELETE FROM notification_events WHERE user_id=request.user_id;
 DELETE FROM notification_preferences WHERE user_id=request.user_id;
 UPDATE telegram_inbox SET payload='{}',state='done',completed_at=now() WHERE actor_telegram_id=request.telegram_user_id;
 UPDATE sheets_connections SET state='revoked',spreadsheet_id='',challenge='' WHERE author_user_id=request.user_id;
 -- Shared originals and financial records remain; discard redundant processing payloads.
 UPDATE ai_jobs SET request='{}' WHERE author_user_id=request.user_id;
 UPDATE voice_jobs SET transcript=NULL,result=NULL,reply=NULL WHERE author_user_id=request.user_id;
 UPDATE receipt_batches SET result=NULL,reply=NULL,categories_snapshot=NULL WHERE author_user_id=request.user_id;
 UPDATE report_jobs SET reply=NULL WHERE author_user_id=request.user_id;
 UPDATE reports SET snapshot='{}',expires_at=now() WHERE author_user_id=request.user_id;
 UPDATE operation_drafts SET state='cancelled' WHERE author_user_id=request.user_id AND state='pending';
 UPDATE memberships SET status='removed' WHERE user_id=request.user_id AND role='participant' AND status<>'removed';
 DELETE FROM service_users WHERE user_id=request.user_id;
 -- The tombstone user UUID preserves shared foreign keys; raw Telegram IDs remain only
 -- in restricted payment records until the operator's published retention policy applies.
 UPDATE memberships SET telegram_user_id=request.anonymous_telegram_id WHERE user_id=request.user_id;
 UPDATE workspace_invitations SET claimant_telegram_id=NULL WHERE claimant_telegram_id=request.telegram_user_id;
 UPDATE workspaces SET owner_telegram_id=request.anonymous_telegram_id WHERE owner_user_id=request.user_id;
 UPDATE fund_transfers SET sender_telegram_id=request.anonymous_telegram_id WHERE sender_user_id=request.user_id;
 UPDATE fund_transfers SET recipient_telegram_id=request.anonymous_telegram_id WHERE recipient_user_id=request.user_id;
 UPDATE fund_claims SET author_telegram_id=request.anonymous_telegram_id WHERE author_user_id=request.user_id;
 UPDATE users SET telegram_user_id=request.anonymous_telegram_id WHERE id=request.user_id;
 UPDATE erasure_requests SET state='files_pending',telegram_user_id=NULL WHERE id=identity;
 SET CONSTRAINTS ALL IMMEDIATE;
 FOREACH t IN ARRAY forced LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
 PERFORM set_config('balans.erasure_worker',coalesce(old_mode,''),true);
 RETURN manifest;
END $$;
REVOKE ALL ON FUNCTION erase_account(uuid) FROM PUBLIC;

-- Payment identities are retained separately, without forcing the user tombstone to keep Telegram ID.
DO $$ DECLARE r record;BEGIN
 FOR r IN SELECT conname,conrelid::regclass AS relation FROM pg_constraint WHERE contype='f' AND confrelid='users'::regclass AND array_length(conkey,1)=2 AND conrelid IN ('billing_accounts'::regclass,'billing_invoices'::regclass,'billing_payments'::regclass) LOOP
 EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',r.relation,r.conname);
 END LOOP;
END $$;
CREATE FUNCTION privacy_access_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN
 IF (SELECT archived FROM workspaces WHERE id=NEW.workspace_id) THEN RAISE EXCEPTION 'Бюджет архивирован; доступна история и экспорт.';END IF;
 IF (SELECT required FROM privacy_policy) AND NOT EXISTS(SELECT 1 FROM user_settings WHERE user_id=actor_user_id() AND service_consent_version=(SELECT version FROM privacy_policy)) THEN RAISE EXCEPTION 'Сначала ознакомьтесь с условиями: /privacy';END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER privacy_operation BEFORE INSERT ON operations FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_draft BEFORE INSERT ON operation_drafts FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_fund BEFORE INSERT ON fund_transfers FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_claim BEFORE INSERT ON fund_claims FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
REVOKE ALL ON FUNCTION privacy_access_guard() FROM PUBLIC;
CREATE TABLE file_purge_queue(id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('receipt','personal')),state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done')),PRIMARY KEY(id,kind));
ALTER TABLE file_purge_queue ENABLE ROW LEVEL SECURITY;ALTER TABLE file_purge_queue FORCE ROW LEVEL SECURITY;
CREATE POLICY worker ON file_purge_queue USING(current_setting('balans.erasure_worker',true)='on') WITH CHECK(current_setting('balans.erasure_worker',true)='on');
CREATE FUNCTION schedule_retention() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE tables text[]:=ARRAY['receipt_files','receipt_batches','user_settings','workspaces','documents','document_quota','file_purge_queue','telegram_updates','voice_jobs','ai_jobs','admin_login_codes','diagnostic_grants'];t text;
BEGIN
 PERFORM pg_advisory_xact_lock(16001001);
 FOREACH t IN ARRAY tables LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 INSERT INTO file_purge_queue(id,kind) SELECT f.id,'receipt' FROM receipt_files f JOIN receipt_batches b ON b.id=f.batch_id JOIN workspaces w ON w.id=f.workspace_id LEFT JOIN user_settings s ON s.user_id=f.author_user_id WHERE f.expires_at<=now() OR (w.kind='personal' AND NOT s.keep_personal_originals AND b.state NOT IN ('collecting','processing')) ON CONFLICT DO NOTHING;
 INSERT INTO file_purge_queue(id,kind) SELECT d.id,'personal' FROM documents d JOIN workspaces w ON w.id=d.workspace_id LEFT JOIN user_settings s ON s.user_id=w.owner_user_id WHERE NOT d.permanent AND (d.expires_at<=now() OR NOT s.keep_personal_originals) ON CONFLICT DO NOTHING;
 UPDATE receipt_files SET expires_at=least(expires_at,now()),telegram_file_id='' WHERE id IN (SELECT id FROM file_purge_queue WHERE kind='receipt');
 UPDATE documents SET state='deleted',expires_at=least(coalesce(expires_at,now()),now()) WHERE id IN (SELECT id FROM file_purge_queue WHERE kind='personal');
 UPDATE document_quota q SET bytes=(SELECT coalesce(sum(size_bytes),0) FROM documents d WHERE d.workspace_id=q.workspace_id AND d.state<>'deleted') WHERE q.workspace_id IN(SELECT id FROM workspaces WHERE kind='personal');
 UPDATE telegram_updates SET response=jsonb_build_object('text','Событие уже обработано. /history — сохранённые операции.'),workspace_id=NULL WHERE created_at<now()-interval '30 days' AND workspace_id IS NOT NULL;
 UPDATE voice_jobs SET transcript=NULL,result=NULL,reply=NULL WHERE created_at<now()-interval '1 day' AND state<>'processing';
 UPDATE ai_jobs SET request='{}' WHERE created_at<now()-interval '30 days' AND state IN ('succeeded','failed','cancelled');
 UPDATE receipt_batches SET result=NULL,reply=NULL,categories_snapshot=NULL WHERE created_at<now()-interval '30 days' AND state NOT IN ('collecting','processing');
 DELETE FROM admin_login_codes WHERE expires_at<now();
 UPDATE diagnostic_grants SET payload='{}',revoked=true WHERE expires_at<now() AND NOT revoked;
 FOREACH t IN ARRAY tables LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
END $$;
REVOKE ALL ON FUNCTION schedule_retention() FROM PUBLIC;
CREATE TRIGGER privacy_revision BEFORE INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_fund_entry BEFORE INSERT ON fund_entries FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_document BEFORE INSERT ON documents FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_ai BEFORE INSERT ON ai_jobs FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_voice BEFORE INSERT ON voice_jobs FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE TRIGGER privacy_receipt BEFORE INSERT ON receipt_batches FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
