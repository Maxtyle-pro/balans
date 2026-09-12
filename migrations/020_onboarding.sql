SET search_path=balans,pg_catalog;
ALTER TABLE billing_config
 ADD COLUMN analysis_quota integer NOT NULL DEFAULT 30 CHECK(analysis_quota>0),
 ADD COLUMN trial_text_quota integer NOT NULL DEFAULT 100 CHECK(trial_text_quota>0),
 ADD COLUMN trial_voice_seconds integer NOT NULL DEFAULT 300 CHECK(trial_voice_seconds>0),
 ADD COLUMN trial_image_quota integer NOT NULL DEFAULT 10 CHECK(trial_image_quota>0),
 ADD COLUMN trial_analysis_quota integer NOT NULL DEFAULT 3 CHECK(trial_analysis_quota>0);
-- Supply the launch defaults only for the previously unconfigured, disabled tariff.
UPDATE billing_config SET text_quota=1000,voice_seconds=3600,image_quota=100
 WHERE NOT enabled AND text_quota=0 AND voice_seconds=0 AND image_quota=0;
ALTER TABLE billing_accounts ADD COLUMN trial_started_at timestamptz, ADD COLUMN trial_until timestamptz,
 ADD COLUMN trial_quotas jsonb, ADD COLUMN trial_terms_version integer;
-- Preserve the original deadline for existing accounts when billing was enabled.
SET LOCAL balans.billing_worker='on';
UPDATE billing_accounts a SET trial_started_at=greatest(a.created_at,c.enabled_at),
 trial_until=greatest(a.created_at,c.enabled_at)+make_interval(days=>c.trial_days),
 trial_quotas=jsonb_build_object('text',c.text_quota,'voice',c.voice_seconds,'image',c.image_quota,'analysis',c.analysis_quota)
 FROM billing_config c WHERE c.enabled;
UPDATE billing_invoices i SET quotas=jsonb_build_object('analysis',c.analysis_quota)||i.quotas FROM billing_config c;
SET LOCAL balans.billing_worker='';
ALTER TABLE notification_preferences ADD COLUMN monthly boolean NOT NULL DEFAULT false,
 ADD COLUMN monthly_enabled_at timestamptz;
ALTER TABLE quota_reservations DROP CONSTRAINT quota_reservations_kind_check;
ALTER TABLE quota_reservations ADD CHECK(kind IN ('text','voice','image','analysis'));
ALTER TABLE quota_reservations ALTER COLUMN period_start TYPE timestamptz USING period_start::timestamp AT TIME ZONE 'UTC';
-- Carry existing usage into its current entitlement period; future reservations
-- use the exact period key so a payment in the same second as trial work resets it.
SET LOCAL balans.billing_worker='on';
ALTER TABLE report_jobs NO FORCE ROW LEVEL SECURITY;
UPDATE quota_reservations q SET kind='analysis' FROM report_jobs j WHERE q.job_id=j.id AND q.kind='text' AND j.kind='analysis';
ALTER TABLE report_jobs FORCE ROW LEVEL SECURITY;
WITH cycles AS (
 SELECT a.user_id,coalesce(p.period_start,
  CASE WHEN a.manual_until>now() THEN a.created_at+floor(greatest(extract(epoch FROM now()-a.created_at),0)/2592000)*interval '30 days'
       ELSE a.trial_started_at END) AS start
 FROM billing_accounts a LEFT JOIN LATERAL
  (SELECT period_start FROM billing_payments WHERE user_id=a.user_id AND success_seen AND NOT refunded AND period_start<=now() AND period_end>now() ORDER BY period_end DESC,created_at DESC LIMIT 1) p ON true
)
UPDATE quota_reservations q SET period_start=cycles.start FROM cycles
 WHERE q.sponsor_id=cycles.user_id AND q.created_at>=cycles.start;
SET LOCAL balans.billing_worker='';


CREATE OR REPLACE FUNCTION billing_access(space uuid DEFAULT current_workspace()) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE cfg billing_config; w workspaces; account billing_accounts; expiry timestamptz; status text;
 old_mode text:=current_setting('balans.billing_worker',true);q jsonb;cycle_start timestamptz;paid_end timestamptz;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=space;IF NOT FOUND THEN RAISE EXCEPTION 'Бюджет недоступен';END IF;
 SELECT * INTO cfg FROM billing_config;
 PERFORM set_config('balans.billing_worker','on',true);
 INSERT INTO billing_accounts(user_id,telegram_user_id) VALUES(w.owner_user_id,w.owner_telegram_id) ON CONFLICT DO NOTHING;
 SELECT * INTO account FROM billing_accounts WHERE user_id=w.owner_user_id;
 IF NOT cfg.enabled THEN
  PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
  RETURN jsonb_build_object('status',CASE WHEN account.suspended THEN 'suspended' ELSE 'disabled' END,'sponsor',w.owner_user_id);
 END IF;
 SELECT max(period_end) INTO expiry FROM billing_payments WHERE user_id=w.owner_user_id AND success_seen AND NOT refunded AND period_start<=now();
 expiry:=greatest(expiry,account.manual_until);
 status:=CASE WHEN account.suspended THEN 'suspended'
  WHEN w.owner_telegram_id=cfg.owner_telegram_id OR EXISTS(SELECT 1 FROM admin_roles WHERE telegram_user_id=w.owner_telegram_id AND active AND role IN ('owner','admin')) THEN 'admin_free'
  WHEN expiry>now() THEN 'active' WHEN account.trial_until>now() THEN 'trial'
  WHEN account.trial_started_at IS NULL AND expiry IS NULL AND cfg.trial_days>0 THEN 'not_started' ELSE 'expired' END;
 SELECT p.period_start,p.period_end,i.quotas INTO cycle_start,paid_end,q
  FROM billing_payments p JOIN billing_invoices i ON i.id=p.invoice_id
  WHERE p.user_id=w.owner_user_id AND p.success_seen AND NOT p.refunded AND p.period_start<=now() AND p.period_end>now()
  ORDER BY p.period_end DESC,p.created_at DESC LIMIT 1;
 IF status='trial' THEN
  cycle_start:=account.trial_started_at;paid_end:=account.trial_until;q:=account.trial_quotas;
 ELSIF status='active' AND cycle_start IS NULL THEN
  cycle_start:=account.created_at+floor(greatest(extract(epoch FROM now()-account.created_at),0)/2592000)*interval '30 days';
  paid_end:=least(cycle_start+interval '30 days',expiry);
 END IF;
 q:=jsonb_build_object('text',cfg.text_quota,'voice',cfg.voice_seconds,'image',cfg.image_quota,'analysis',cfg.analysis_quota)||coalesce(q,'{}'::jsonb);
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
 RETURN jsonb_build_object('status',status,'until',CASE WHEN status='trial' THEN account.trial_until WHEN status='expired' THEN greatest(account.trial_until,expiry) ELSE expiry END,
  'sponsor',w.owner_user_id,'quotas',q,'period_start',cycle_start,'period_end',paid_end);
END $$;
CREATE OR REPLACE FUNCTION require_paid_access(space uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE status text:=billing_access(space)->>'status';
BEGIN
 IF status='not_started' THEN RAISE EXCEPTION 'Сначала начните бесплатный период: /start. Для общего бюджета это делает его владелец.';END IF;
 IF status IN ('expired','suspended') THEN RAISE EXCEPTION 'Доступ к новым операциям завершён. История и экспорт доступны. /subscription — подписка владельца бюджета.';END IF;
END $$;
CREATE OR REPLACE FUNCTION reserve_quota(job uuid,space uuid,author uuid,kind_name text,quantity integer) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE access jsonb;sponsor uuid;used bigint;maximum integer;existing integer:=0;
 old_mode text:=current_setting('balans.billing_worker',true);cycle_start timestamptz;
BEGIN
 access:=billing_access(space);IF access->>'status' IN ('disabled','admin_free') THEN RETURN;END IF;
 PERFORM require_paid_access(space);sponsor:=(access->>'sponsor')::uuid;cycle_start:=(access->>'period_start')::timestamptz;
 IF quantity<=0 OR kind_name NOT IN ('text','voice','image','analysis') THEN RAISE EXCEPTION 'Некорректная квота';END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(sponsor::text,14001));PERFORM set_config('balans.billing_worker','on',true);
 SELECT units INTO existing FROM quota_reservations WHERE job_id=job AND kind=kind_name AND state<>'released';
 IF FOUND AND existing>=quantity THEN PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN;END IF;
 maximum:=(access->'quotas'->>kind_name)::integer;
 SELECT coalesce(sum(units),0) INTO used FROM quota_reservations WHERE sponsor_id=sponsor AND kind=kind_name
  AND period_start=cycle_start AND state IN ('reserved','consumed') AND job_id<>job;
 IF maximum IS NULL OR cycle_start IS NULL OR used+quantity>maximum THEN RAISE EXCEPTION 'Квота AI исчерпана (%). /subscription — лимиты. Ручной ввод доступен.',kind_name;END IF;
 INSERT INTO quota_reservations(job_id,kind,workspace_id,sponsor_id,author_user_id,units,period_start,state)
  VALUES(job,kind_name,space,sponsor,author,quantity,cycle_start,'reserved')
  ON CONFLICT(job_id,kind) DO UPDATE SET state='reserved',period_start=excluded.period_start,units=excluded.units,created_at=now();
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);
END $$;
CREATE OR REPLACE FUNCTION track_job_quota() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE k text;quantity integer:=1;old_mode text:=current_setting('balans.billing_worker',true);
BEGIN
 k:=CASE TG_TABLE_NAME WHEN 'voice_jobs' THEN 'voice' WHEN 'receipt_batches' THEN 'image' WHEN 'report_jobs' THEN 'analysis' ELSE 'text' END;
 IF TG_TABLE_NAME='report_jobs' THEN IF NEW.kind<>'analysis' THEN RETURN NEW;END IF;END IF;
 IF TG_TABLE_NAME='voice_jobs' THEN quantity:=NEW.duration_seconds;END IF;
 IF TG_TABLE_NAME='receipt_batches' THEN SELECT greatest(coalesce(sum(page_count),0),1)::integer INTO quantity FROM receipt_files WHERE batch_id=NEW.id;END IF;
 IF NEW.state IN ('queued','running','pending','processing') THEN PERFORM reserve_quota(NEW.id,NEW.workspace_id,NEW.author_user_id,k,quantity);END IF;
 PERFORM set_config('balans.billing_worker','on',true);
 IF NEW.state IN ('failed','cancelled') THEN UPDATE quota_reservations SET state='released' WHERE job_id=NEW.id AND kind=k AND state='reserved';
 ELSIF NEW.state IN ('ready','succeeded') THEN UPDATE quota_reservations SET state='consumed' WHERE job_id=NEW.id AND kind=k AND state='reserved';END IF;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN NEW;
END $$;
CREATE FUNCTION billing_usage() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE access jsonb:=billing_access();result jsonb;old_mode text:=current_setting('balans.billing_worker',true);
BEGIN
 PERFORM set_config('balans.billing_worker','on',true);
 SELECT coalesce(jsonb_object_agg(kind,used),'{}'::jsonb) INTO result FROM
  (SELECT kind,sum(units) AS used FROM quota_reservations WHERE sponsor_id=(access->>'sponsor')::uuid
   AND period_start=(access->>'period_start')::timestamptz
   AND state IN ('reserved','consumed') GROUP BY kind) totals;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN result;
END $$;
REVOKE ALL ON FUNCTION billing_usage() FROM PUBLIC;
