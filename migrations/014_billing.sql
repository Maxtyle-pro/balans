SET search_path=balans,pg_catalog;
CREATE TABLE billing_config(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),enabled boolean NOT NULL DEFAULT false,enabled_at timestamptz NOT NULL DEFAULT now(),owner_telegram_id bigint,stars integer CHECK(stars BETWEEN 1 AND 10000),trial_days integer NOT NULL DEFAULT 7 CHECK(trial_days BETWEEN 0 AND 90),text_quota integer NOT NULL DEFAULT 0 CHECK(text_quota>=0),voice_seconds integer NOT NULL DEFAULT 0 CHECK(voice_seconds>=0),image_quota integer NOT NULL DEFAULT 0 CHECK(image_quota>=0),terms_url text NOT NULL DEFAULT '',terms_version integer NOT NULL DEFAULT 1,support_contact text NOT NULL DEFAULT '');
INSERT INTO billing_config DEFAULT VALUES;
CREATE TABLE billing_accounts(user_id uuid PRIMARY KEY REFERENCES users,telegram_user_id bigint UNIQUE NOT NULL,suspended boolean NOT NULL DEFAULT false,manual_until timestamptz,created_at timestamptz NOT NULL DEFAULT now(),FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
CREATE TABLE billing_invoices(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,stars integer NOT NULL CHECK(stars>0),terms_version integer NOT NULL,terms_url text NOT NULL,quotas jsonb NOT NULL,accepted_at timestamptz NOT NULL DEFAULT now(),created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '1 hour',checkout_id text,checkout_until timestamptz,invoice_url text,first_charge_id text,auto_renew boolean NOT NULL DEFAULT true,FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
CREATE TABLE billing_payments(charge_id text PRIMARY KEY,invoice_id uuid NOT NULL REFERENCES billing_invoices,user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,stars integer NOT NULL,currency text NOT NULL CHECK(currency='XTR'),period_start timestamptz,period_end timestamptz,refunded boolean NOT NULL DEFAULT false,success_seen boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now(),FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
CREATE TABLE billing_audit(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,event_key text UNIQUE NOT NULL,action text NOT NULL,details jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE quota_reservations(job_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('text','voice','image')),workspace_id uuid NOT NULL REFERENCES workspaces,sponsor_id uuid NOT NULL REFERENCES users,author_user_id uuid NOT NULL REFERENCES users,units integer NOT NULL CHECK(units>0),period_start date NOT NULL,state text NOT NULL CHECK(state IN ('reserved','consumed','released')),created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(job_id,kind));
ALTER TABLE voice_jobs ADD COLUMN duration_seconds integer NOT NULL DEFAULT 180 CHECK(duration_seconds BETWEEN 1 AND 180);
DO $$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['billing_accounts','billing_invoices','billing_payments','billing_audit'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY own ON %I USING(user_id=actor_user_id() OR current_setting(''balans.billing_worker'',true)=''on'') WITH CHECK(user_id=actor_user_id() OR current_setting(''balans.billing_worker'',true)=''on'')',t);
END LOOP;END $$;
ALTER TABLE quota_reservations ENABLE ROW LEVEL SECURITY;ALTER TABLE quota_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON quota_reservations USING(sponsor_id=actor_user_id() OR author_user_id=actor_user_id() OR current_setting('balans.billing_worker',true)='on') WITH CHECK(current_setting('balans.billing_worker',true)='on');
CREATE FUNCTION billing_access(space uuid DEFAULT current_workspace()) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE cfg billing_config; w workspaces; account billing_accounts; expiry timestamptz; trial_end timestamptz; status text;old_mode text:=current_setting('balans.billing_worker',true);q jsonb;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=space;IF NOT FOUND THEN RAISE EXCEPTION 'Бюджет недоступен';END IF;
 SELECT * INTO cfg FROM billing_config;
 IF NOT cfg.enabled THEN RETURN jsonb_build_object('status','disabled','sponsor',w.owner_user_id);END IF;
 PERFORM set_config('balans.billing_worker','on',true);
 INSERT INTO billing_accounts(user_id,telegram_user_id) VALUES(w.owner_user_id,w.owner_telegram_id) ON CONFLICT DO NOTHING;
 SELECT * INTO account FROM billing_accounts WHERE user_id=w.owner_user_id;
 SELECT max(period_end) INTO expiry FROM billing_payments WHERE user_id=w.owner_user_id AND success_seen AND NOT refunded AND period_start<=now();
 expiry:=greatest(expiry,account.manual_until);trial_end:=greatest(account.created_at,cfg.enabled_at)+make_interval(days=>cfg.trial_days);
 status:=CASE WHEN account.suspended THEN 'suspended' WHEN w.owner_telegram_id=cfg.owner_telegram_id THEN 'admin_free' WHEN expiry>now() THEN 'active' WHEN trial_end>now() THEN 'trial' ELSE 'expired' END;
 SELECT i.quotas INTO q FROM billing_payments p JOIN billing_invoices i ON i.id=p.invoice_id WHERE p.user_id=w.owner_user_id AND p.success_seen AND NOT p.refunded AND p.period_start<=now() AND p.period_end>now() ORDER BY p.period_end DESC LIMIT 1;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
 RETURN jsonb_build_object('status',status,'until',CASE WHEN status IN ('trial','expired') THEN greatest(trial_end,expiry) ELSE expiry END,'sponsor',w.owner_user_id,'quotas',coalesce(q,jsonb_build_object('text',cfg.text_quota,'voice',cfg.voice_seconds,'image',cfg.image_quota)));
END $$;
CREATE FUNCTION require_paid_access(space uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN IF billing_access(space)->>'status' IN ('expired','suspended') THEN RAISE EXCEPTION 'Доступ к новым операциям завершён. История и экспорт доступны. /subscription — подписка владельца бюджета.';END IF;END $$;
CREATE FUNCTION guard_paid_creation() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN PERFORM require_paid_access(NEW.workspace_id);RETURN NEW;END $$;
CREATE TRIGGER billing_operation BEFORE INSERT ON operations FOR EACH ROW EXECUTE FUNCTION guard_paid_creation();
CREATE TRIGGER billing_draft BEFORE INSERT ON operation_drafts FOR EACH ROW EXECUTE FUNCTION guard_paid_creation();
CREATE TRIGGER billing_transfer BEFORE INSERT ON fund_transfers FOR EACH ROW EXECUTE FUNCTION guard_paid_creation();
CREATE TRIGGER billing_claim BEFORE INSERT ON fund_claims FOR EACH ROW EXECUTE FUNCTION guard_paid_creation();
CREATE FUNCTION reserve_quota(job uuid,space uuid,author uuid,kind_name text,quantity integer) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE access jsonb; sponsor uuid;used bigint;maximum integer;old_mode text:=current_setting('balans.billing_worker',true);month_start date:=date_trunc('month',now() AT TIME ZONE 'UTC')::date;
BEGIN
 access:=billing_access(space);IF access->>'status' IN ('disabled','admin_free') THEN RETURN;END IF;
 PERFORM require_paid_access(space);sponsor:=(access->>'sponsor')::uuid;
 PERFORM pg_advisory_xact_lock(hashtextextended(sponsor::text,14001));PERFORM set_config('balans.billing_worker','on',true);
 IF EXISTS(SELECT 1 FROM quota_reservations WHERE job_id=job AND kind=kind_name AND state<>'released') THEN PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN;END IF;
 maximum:=(access->'quotas'->>kind_name)::integer;
 SELECT coalesce(sum(units),0) INTO used FROM quota_reservations WHERE sponsor_id=sponsor AND kind=kind_name AND period_start=month_start AND state IN ('reserved','consumed');
 IF used+quantity>maximum THEN RAISE EXCEPTION 'Квота AI исчерпана (%). /subscription — лимиты. Ручной ввод доступен.',kind_name;END IF;
 INSERT INTO quota_reservations(job_id,kind,workspace_id,sponsor_id,author_user_id,units,period_start,state) VALUES(job,kind_name,space,sponsor,author,quantity,month_start,'reserved') ON CONFLICT(job_id,kind) DO UPDATE SET state='reserved',period_start=excluded.period_start,units=excluded.units;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
END $$;
CREATE FUNCTION track_job_quota() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE k text;quantity integer:=1;old_mode text:=current_setting('balans.billing_worker',true);
BEGIN
 k:=CASE TG_TABLE_NAME WHEN 'voice_jobs' THEN 'voice' WHEN 'receipt_batches' THEN 'image' ELSE 'text' END;
 IF TG_TABLE_NAME='report_jobs' THEN IF NEW.kind<>'analysis' THEN RETURN NEW;END IF;END IF;
 IF TG_TABLE_NAME='voice_jobs' THEN quantity:=NEW.duration_seconds;END IF;
 IF TG_TABLE_NAME='receipt_batches' THEN SELECT greatest(count(*),1)::integer INTO quantity FROM receipt_files WHERE batch_id=NEW.id;END IF;
 IF NEW.state IN ('queued','running','pending','processing') THEN PERFORM reserve_quota(NEW.id,NEW.workspace_id,NEW.author_user_id,k,quantity);END IF;
 PERFORM set_config('balans.billing_worker','on',true);
 IF NEW.state IN ('failed','cancelled') THEN UPDATE quota_reservations SET state='released' WHERE job_id=NEW.id AND kind=k AND state='reserved';
 ELSIF NEW.state IN ('ready','succeeded') THEN UPDATE quota_reservations SET state='consumed' WHERE job_id=NEW.id AND kind=k AND state='reserved';END IF;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN NEW;
END $$;
CREATE TRIGGER quota_ai AFTER INSERT OR UPDATE OF state ON ai_jobs FOR EACH ROW EXECUTE FUNCTION track_job_quota();
CREATE TRIGGER quota_voice AFTER INSERT OR UPDATE OF state ON voice_jobs FOR EACH ROW EXECUTE FUNCTION track_job_quota();
CREATE TRIGGER quota_receipt AFTER INSERT OR UPDATE OF state ON receipt_batches FOR EACH ROW EXECUTE FUNCTION track_job_quota();
CREATE TRIGGER quota_report AFTER INSERT OR UPDATE OF state ON report_jobs FOR EACH ROW EXECUTE FUNCTION track_job_quota();
REVOKE ALL ON FUNCTION billing_access(uuid),require_paid_access(uuid),guard_paid_creation(),reserve_quota(uuid,uuid,uuid,text,integer),track_job_quota() FROM PUBLIC;
-- Polling must keep accepting pre-checkout queries while AI is running.
CREATE TABLE telegram_inbox(bot_id bigint NOT NULL,update_id bigint NOT NULL,actor_telegram_id bigint,payload jsonb NOT NULL,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','done','failed')),attempts integer NOT NULL DEFAULT 0,available_at timestamptz NOT NULL DEFAULT now(),lease_until timestamptz,error_code text,created_at timestamptz NOT NULL DEFAULT now(),completed_at timestamptz,PRIMARY KEY(bot_id,update_id));
ALTER TABLE telegram_inbox ENABLE ROW LEVEL SECURITY;ALTER TABLE telegram_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY worker ON telegram_inbox USING(current_setting('balans.inbox_worker',true)='on') WITH CHECK(current_setting('balans.inbox_worker',true)='on');
CREATE INDEX telegram_inbox_due ON telegram_inbox(bot_id,available_at,update_id) WHERE state IN ('pending','processing');
CREATE TABLE billing_refund_requests(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),charge_id text NOT NULL REFERENCES billing_payments,user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,requested_by bigint NOT NULL,reason text NOT NULL CHECK(length(reason) BETWEEN 3 AND 1000),state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','succeeded','uncertain')),created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX one_refund_request ON billing_refund_requests(charge_id);
ALTER TABLE billing_refund_requests ENABLE ROW LEVEL SECURITY;ALTER TABLE billing_refund_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY operator ON billing_refund_requests USING(actor_telegram_id()=(SELECT owner_telegram_id FROM billing_config)) WITH CHECK(actor_telegram_id()=(SELECT owner_telegram_id FROM billing_config));
