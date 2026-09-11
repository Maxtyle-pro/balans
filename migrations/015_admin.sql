SET search_path=balans,pg_catalog;
CREATE TABLE admin_roles(telegram_user_id bigint PRIMARY KEY,role text NOT NULL CHECK(role IN ('owner','admin','support')),active boolean NOT NULL DEFAULT true);
CREATE FUNCTION admin_role() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=balans,pg_temp AS $$ SELECT role FROM admin_roles WHERE telegram_user_id=actor_telegram_id() AND active $$;
CREATE TABLE admin_login_codes(token_hash text PRIMARY KEY,telegram_user_id bigint NOT NULL,expires_at timestamptz NOT NULL DEFAULT now()+interval '2 minutes',consumed boolean NOT NULL DEFAULT false);
CREATE TABLE admin_sessions(token_hash text PRIMARY KEY,telegram_user_id bigint NOT NULL,csrf text NOT NULL,expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes',verified_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE admin_otp_used(telegram_user_id bigint NOT NULL,counter bigint NOT NULL,PRIMARY KEY(telegram_user_id,counter));
CREATE TABLE admin_audit(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),actor_telegram_id bigint NOT NULL,action text NOT NULL,target text,reason text,details jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE service_users(user_id uuid PRIMARY KEY,telegram_user_id bigint UNIQUE NOT NULL,status text NOT NULL,created_at timestamptz NOT NULL,last_seen timestamptz);
CREATE TABLE admin_job_metrics(job_id uuid PRIMARY KEY,kind text NOT NULL,author_user_id uuid,workspace_id uuid,state text NOT NULL,model text,prompt_version text,error_code text,input_tokens integer,output_tokens integer,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE service_content(key text PRIMARY KEY,value text NOT NULL CHECK(length(value)<=3500),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE ai_configuration(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),model text,transcribe_model text,confidence numeric CHECK(confidence BETWEEN 0 AND 1),fallback_model text,input_usd_per_million numeric CHECK(input_usd_per_million>=0),output_usd_per_million numeric CHECK(output_usd_per_million>=0),monthly_cost_limit_usd numeric CHECK(monthly_cost_limit_usd>0),updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO ai_configuration DEFAULT VALUES;
CREATE TABLE support_tickets(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,body text NOT NULL CHECK(length(body) BETWEEN 1 AND 3000),reply text CHECK(length(reply)<=3000),state text NOT NULL DEFAULT 'open' CHECK(state IN ('open','answered','closed')),created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE diagnostic_grants(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,workspace_id uuid NOT NULL REFERENCES workspaces,admin_telegram_id bigint NOT NULL,operation_id uuid NOT NULL,payload jsonb NOT NULL,confirmed boolean NOT NULL DEFAULT false,revoked boolean NOT NULL DEFAULT false,expires_at timestamptz NOT NULL DEFAULT now()+interval '15 minutes',created_at timestamptz NOT NULL DEFAULT now());
DO $$ DECLARE t text;BEGIN FOREACH t IN ARRAY ARRAY['admin_login_codes','admin_sessions','admin_otp_used'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY auth ON %I USING(current_setting(''balans.admin_auth'',true)=''on'') WITH CHECK(current_setting(''balans.admin_auth'',true)=''on'')',t);
END LOOP;
FOREACH t IN ARRAY ARRAY['admin_audit','service_users','admin_job_metrics'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY admin ON %I USING(admin_role() IS NOT NULL OR current_setting(''balans.admin_metrics'',true)=''on'') WITH CHECK(admin_role() IS NOT NULL OR current_setting(''balans.admin_metrics'',true)=''on'')',t);
END LOOP;END $$;
ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;ALTER TABLE support_tickets FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON support_tickets USING(user_id=actor_user_id() OR admin_role() IS NOT NULL) WITH CHECK(user_id=actor_user_id() OR admin_role() IS NOT NULL);
ALTER TABLE diagnostic_grants ENABLE ROW LEVEL SECURITY;ALTER TABLE diagnostic_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON diagnostic_grants USING(user_id=actor_user_id() OR (admin_telegram_id=actor_telegram_id() AND admin_role() IS NOT NULL AND confirmed AND NOT revoked AND expires_at>now() AND workspace_id IN (SELECT id FROM workspaces))) WITH CHECK(user_id=actor_user_id());
-- Admins cannot see a user's workspace. Diagnostic snapshots use a separate explicit grant,
-- validated against the grantor's current access by the application on every read.
DROP POLICY own ON diagnostic_grants;
CREATE POLICY own ON diagnostic_grants USING(user_id=actor_user_id() OR (admin_telegram_id=actor_telegram_id() AND admin_role() IS NOT NULL AND confirmed AND NOT revoked AND expires_at>now())) WITH CHECK(user_id=actor_user_id());
CREATE FUNCTION track_service_user() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE old_mode text:=current_setting('balans.admin_metrics',true);BEGIN
 PERFORM set_config('balans.admin_metrics','on',true);
 INSERT INTO service_users(user_id,telegram_user_id,status,created_at) VALUES(NEW.id,NEW.telegram_user_id,NEW.status,NEW.created_at) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status;
 PERFORM set_config('balans.admin_metrics',coalesce(old_mode,''),true);RETURN NEW;END $$;
CREATE TRIGGER service_user AFTER INSERT OR UPDATE OF status ON users FOR EACH ROW EXECUTE FUNCTION track_service_user();
ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
SELECT set_config('balans.admin_metrics','on',true);
INSERT INTO service_users(user_id,telegram_user_id,status,created_at) SELECT id,telegram_user_id,status,created_at FROM users;
SELECT set_config('balans.admin_metrics','',true);
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE FUNCTION track_admin_job() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE old_mode text:=current_setting('balans.admin_metrics',true);data jsonb:=to_jsonb(NEW);BEGIN
 PERFORM set_config('balans.admin_metrics','on',true);
 INSERT INTO admin_job_metrics(job_id,kind,author_user_id,workspace_id,state,model,prompt_version,error_code,input_tokens,output_tokens,created_at) VALUES(NEW.id,TG_TABLE_NAME,NEW.author_user_id,NEW.workspace_id,NEW.state,data->>'model',data->>'prompt_version',data->>'error_code',(data->>'input_tokens')::integer,(data->>'output_tokens')::integer,NEW.created_at) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,error_code=excluded.error_code,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,updated_at=now();
 PERFORM set_config('balans.admin_metrics',coalesce(old_mode,''),true);RETURN NEW;END $$;
CREATE TRIGGER admin_job_ai AFTER INSERT OR UPDATE ON ai_jobs FOR EACH ROW EXECUTE FUNCTION track_admin_job();
CREATE TRIGGER admin_job_voice AFTER INSERT OR UPDATE ON voice_jobs FOR EACH ROW EXECUTE FUNCTION track_admin_job();
CREATE TRIGGER admin_job_receipt AFTER INSERT OR UPDATE ON receipt_batches FOR EACH ROW EXECUTE FUNCTION track_admin_job();
CREATE TRIGGER admin_job_report AFTER INSERT OR UPDATE ON report_jobs FOR EACH ROW EXECUTE FUNCTION track_admin_job();
REVOKE ALL ON FUNCTION admin_role(),track_service_user(),track_admin_job() FROM PUBLIC;
ALTER TABLE admin_login_codes ADD COLUMN attempts integer NOT NULL DEFAULT 0;
DO $$ DECLARE t text;BEGIN FOREACH t IN ARRAY ARRAY['billing_config','service_content','ai_configuration','admin_roles'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY read ON %I FOR SELECT USING(true)',t);
 EXECUTE format('CREATE POLICY edit ON %I FOR ALL USING(admin_role()=''owner'') WITH CHECK(admin_role()=''owner'')',t);
END LOOP;END $$;
CREATE FUNCTION record_activity() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE old_mode text:=current_setting('balans.admin_metrics',true);BEGIN
 PERFORM set_config('balans.admin_metrics','on',true);UPDATE service_users SET last_seen=now() WHERE user_id=actor_user_id();PERFORM set_config('balans.admin_metrics',coalesce(old_mode,''),true);
END $$;
REVOKE ALL ON FUNCTION record_activity() FROM PUBLIC;
DO $$ DECLARE t text;BEGIN
 PERFORM set_config('balans.admin_metrics','on',true);
 FOREACH t IN ARRAY ARRAY['ai_jobs','voice_jobs','receipt_batches','report_jobs'] LOOP
 EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('INSERT INTO admin_job_metrics(job_id,kind,author_user_id,workspace_id,state,model,prompt_version,error_code,input_tokens,output_tokens,created_at) SELECT id,%L,author_user_id,workspace_id,state,to_jsonb(j)->>''model'',to_jsonb(j)->>''prompt_version'',to_jsonb(j)->>''error_code'',(to_jsonb(j)->>''input_tokens'')::integer,(to_jsonb(j)->>''output_tokens'')::integer,created_at FROM %I j ON CONFLICT DO NOTHING',t,t);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 END LOOP;
 PERFORM set_config('balans.admin_metrics','',true);
END $$;
ALTER TABLE workspaces ADD COLUMN content_defaults_applied boolean NOT NULL DEFAULT false;
ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;
UPDATE workspaces SET content_defaults_applied=true;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE FUNCTION apply_content_defaults() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE item text;w uuid:=current_workspace();BEGIN
 IF NOT owns_workspace(w) OR (SELECT content_defaults_applied FROM workspaces WHERE id=w) THEN RETURN;END IF;
 UPDATE workspaces SET content_defaults_applied=true WHERE id=w;
 FOR item IN SELECT btrim(x) FROM regexp_split_to_table(coalesce((SELECT value FROM service_content WHERE key='base_categories'),''),E'\n') x LOOP
 IF length(item) BETWEEN 1 AND 60 AND NOT EXISTS(SELECT 1 FROM categories WHERE name=item) AND (SELECT count(*) FROM categories WHERE NOT archived)<30 THEN PERFORM manage_category('add',item,NULL);END IF;
 END LOOP;
END $$;
ALTER FUNCTION bootstrap() RENAME TO bootstrap_before_admin;
CREATE FUNCTION bootstrap() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE identity uuid;BEGIN identity:=bootstrap_before_admin();PERFORM apply_content_defaults();RETURN identity;END $$;
ALTER FUNCTION create_workspace(text) RENAME TO create_workspace_before_admin;
CREATE FUNCTION create_workspace(workspace_name text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ DECLARE identity uuid;BEGIN identity:=create_workspace_before_admin(workspace_name);PERFORM apply_content_defaults();RETURN identity;END $$;
REVOKE ALL ON FUNCTION bootstrap_before_admin(),create_workspace_before_admin(text),apply_content_defaults(),bootstrap(),create_workspace(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION billing_access(space uuid DEFAULT current_workspace()) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE cfg billing_config; w workspaces; account billing_accounts; expiry timestamptz; trial_end timestamptz; status text;old_mode text:=current_setting('balans.billing_worker',true);q jsonb;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=space;IF NOT FOUND THEN RAISE EXCEPTION 'Бюджет недоступен';END IF;
 SELECT * INTO cfg FROM billing_config;

 PERFORM set_config('balans.billing_worker','on',true);
 INSERT INTO billing_accounts(user_id,telegram_user_id) VALUES(w.owner_user_id,w.owner_telegram_id) ON CONFLICT DO NOTHING;
 SELECT * INTO account FROM billing_accounts WHERE user_id=w.owner_user_id;
 IF NOT cfg.enabled THEN PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN jsonb_build_object('status',CASE WHEN account.suspended THEN 'suspended' ELSE 'disabled' END,'sponsor',w.owner_user_id);END IF;
 SELECT max(period_end) INTO expiry FROM billing_payments WHERE user_id=w.owner_user_id AND success_seen AND NOT refunded AND period_start<=now();
 expiry:=greatest(expiry,account.manual_until);trial_end:=greatest(account.created_at,cfg.enabled_at)+make_interval(days=>cfg.trial_days);
 status:=CASE WHEN account.suspended THEN 'suspended' WHEN w.owner_telegram_id=cfg.owner_telegram_id OR EXISTS(SELECT 1 FROM admin_roles WHERE telegram_user_id=w.owner_telegram_id AND active AND role IN ('owner','admin')) THEN 'admin_free' WHEN expiry>now() THEN 'active' WHEN trial_end>now() THEN 'trial' ELSE 'expired' END;
 SELECT i.quotas INTO q FROM billing_payments p JOIN billing_invoices i ON i.id=p.invoice_id WHERE p.user_id=w.owner_user_id AND p.success_seen AND NOT p.refunded AND p.period_start<=now() AND p.period_end>now() ORDER BY p.period_end DESC LIMIT 1;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
 RETURN jsonb_build_object('status',status,'until',CASE WHEN status IN ('trial','expired') THEN greatest(trial_end,expiry) ELSE expiry END,'sponsor',w.owner_user_id,'quotas',coalesce(q,jsonb_build_object('text',cfg.text_quota,'voice',cfg.voice_seconds,'image',cfg.image_quota)));
END $$;
ALTER TABLE ai_configuration ADD COLUMN text_usd_per_request numeric CHECK(text_usd_per_request>0),ADD COLUMN voice_usd_per_minute numeric CHECK(voice_usd_per_minute>0),ADD COLUMN image_usd_per_file numeric CHECK(image_usd_per_file>0);
CREATE TABLE ai_cost_reservations(job_id uuid PRIMARY KEY,kind text NOT NULL,estimated_usd numeric NOT NULL CHECK(estimated_usd>=0),period_start date NOT NULL,state text NOT NULL CHECK(state IN ('reserved','consumed','released')));
ALTER TABLE ai_cost_reservations ENABLE ROW LEVEL SECURITY;ALTER TABLE ai_cost_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY metrics ON ai_cost_reservations USING(admin_role() IS NOT NULL OR current_setting('balans.admin_metrics',true)='on') WITH CHECK(current_setting('balans.admin_metrics',true)='on');
CREATE FUNCTION guard_ai_estimated_cost() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE cfg ai_configuration;kind_name text;estimate numeric;used numeric;old_mode text:=current_setting('balans.admin_metrics',true);month_start date:=date_trunc('month',now() AT TIME ZONE 'UTC')::date;
BEGIN
 IF TG_TABLE_NAME='report_jobs' THEN IF NEW.kind<>'analysis' THEN RETURN NEW;END IF;END IF;
 SELECT * INTO cfg FROM ai_configuration;
 PERFORM set_config('balans.admin_metrics','on',true);
 IF NEW.state IN ('failed','cancelled') THEN UPDATE ai_cost_reservations SET state='consumed' WHERE job_id=NEW.id AND state='reserved';
 ELSIF NEW.state IN ('ready','succeeded') THEN UPDATE ai_cost_reservations SET state='consumed' WHERE job_id=NEW.id AND state='reserved';
 ELSIF NEW.state IN ('queued','running','pending','processing') AND cfg.monthly_cost_limit_usd IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(15001001);
  IF NOT EXISTS(SELECT 1 FROM ai_cost_reservations WHERE job_id=NEW.id AND state<>'released') THEN
   kind_name:=CASE TG_TABLE_NAME WHEN 'voice_jobs' THEN 'voice' WHEN 'receipt_batches' THEN 'image' ELSE 'text' END;
   IF kind_name='voice' THEN estimate:=cfg.voice_usd_per_minute*NEW.duration_seconds/60;
   ELSIF kind_name='image' THEN SELECT cfg.image_usd_per_file*greatest(count(*),1) INTO estimate FROM receipt_files WHERE batch_id=NEW.id;
   ELSE estimate:=cfg.text_usd_per_request;END IF;
   IF estimate IS NULL THEN RAISE EXCEPTION 'Не настроена оценка стоимости AI; ручной ввод доступен.';END IF;
   SELECT coalesce(sum(estimated_usd),0) INTO used FROM ai_cost_reservations WHERE period_start=month_start AND state IN ('reserved','consumed');
   IF used+estimate>cfg.monthly_cost_limit_usd THEN RAISE EXCEPTION 'Достигнут технический лимит оценочной стоимости AI. Ручной ввод доступен.';END IF;
   INSERT INTO ai_cost_reservations VALUES(NEW.id,kind_name,estimate,month_start,'reserved') ON CONFLICT(job_id) DO UPDATE SET state='reserved',period_start=excluded.period_start,estimated_usd=excluded.estimated_usd;
  END IF;
 END IF;
 PERFORM set_config('balans.admin_metrics',coalesce(old_mode,''),true);RETURN NEW;
END $$;
CREATE TRIGGER cost_ai AFTER INSERT OR UPDATE OF state ON ai_jobs FOR EACH ROW EXECUTE FUNCTION guard_ai_estimated_cost();
CREATE TRIGGER cost_voice AFTER INSERT OR UPDATE OF state ON voice_jobs FOR EACH ROW EXECUTE FUNCTION guard_ai_estimated_cost();
CREATE TRIGGER cost_receipt AFTER INSERT OR UPDATE OF state ON receipt_batches FOR EACH ROW EXECUTE FUNCTION guard_ai_estimated_cost();
CREATE TRIGGER cost_report AFTER INSERT OR UPDATE OF state ON report_jobs FOR EACH ROW EXECUTE FUNCTION guard_ai_estimated_cost();
REVOKE ALL ON FUNCTION guard_ai_estimated_cost() FROM PUBLIC;
