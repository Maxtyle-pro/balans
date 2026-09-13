SET search_path=balans,pg_catalog;
ALTER TABLE receipt_files ADD COLUMN retention_warned_at timestamptz;
ALTER TABLE documents ADD COLUMN retention_warned_at timestamptz;
ALTER TABLE receipt_files ALTER COLUMN expires_at SET DEFAULT now()+interval '90 days';
-- Existing files receive at least ninety days from rollout; purged files stay purged.
ALTER TABLE receipt_files NO FORCE ROW LEVEL SECURITY;
ALTER TABLE documents NO FORCE ROW LEVEL SECURITY;
ALTER TABLE file_purge_queue NO FORCE ROW LEVEL SECURITY;
UPDATE receipt_files SET expires_at=greatest(expires_at,now()+interval '90 days') WHERE telegram_file_id<>'' AND NOT EXISTS(SELECT 1 FROM file_purge_queue q WHERE q.id=receipt_files.id);
UPDATE documents SET expires_at=greatest(coalesce(expires_at,now()),now()+interval '90 days') WHERE state<>'deleted' AND NOT EXISTS(SELECT 1 FROM file_purge_queue q WHERE q.id=documents.id);
ALTER TABLE receipt_files FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE file_purge_queue FORCE ROW LEVEL SECURITY;
ALTER TABLE file_purge_queue DROP CONSTRAINT file_purge_queue_kind_check;
ALTER TABLE file_purge_queue ADD CHECK(kind IN ('receipt','personal','shared'));
CREATE FUNCTION file_expiry_default() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.expires_at:=now()+interval '90 days';RETURN NEW;END $$;
CREATE TRIGGER file_expiry BEFORE INSERT ON documents FOR EACH ROW EXECUTE FUNCTION file_expiry_default();
CREATE TABLE retention_batches(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,user_id uuid NOT NULL REFERENCES users,deadline timestamptz NOT NULL,files jsonb NOT NULL,notice_sent_at timestamptz,extended_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(workspace_id,deadline));
ALTER TABLE retention_batches ENABLE ROW LEVEL SECURITY;ALTER TABLE retention_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON retention_batches USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());

CREATE OR REPLACE FUNCTION schedule_retention() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE tables text[]:=ARRAY['receipt_files','receipt_batches','user_settings','workspaces','documents','document_quota','file_purge_queue','telegram_updates','voice_jobs','ai_jobs','admin_login_codes','diagnostic_grants'];t text;
BEGIN
 PERFORM pg_advisory_xact_lock(16001001);
 FOREACH t IN ARRAY tables LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 INSERT INTO file_purge_queue(id,kind) SELECT f.id,'receipt' FROM receipt_files f JOIN receipt_batches b ON b.id=f.batch_id JOIN workspaces w ON w.id=f.workspace_id LEFT JOIN user_settings s ON s.user_id=f.author_user_id WHERE (f.expires_at<=now() AND f.retention_warned_at<=now()-interval '7 days') OR (w.kind='personal' AND NOT s.keep_personal_originals AND b.state NOT IN ('collecting','processing')) ON CONFLICT DO NOTHING;
 INSERT INTO file_purge_queue(id,kind) SELECT d.id,CASE WHEN d.permanent THEN 'shared' ELSE 'personal' END FROM documents d JOIN workspaces w ON w.id=d.workspace_id LEFT JOIN user_settings s ON s.user_id=w.owner_user_id WHERE (d.expires_at<=now() AND d.retention_warned_at<=now()-interval '7 days') OR (NOT d.permanent AND NOT s.keep_personal_originals) ON CONFLICT DO NOTHING;
 UPDATE receipt_files SET expires_at=least(expires_at,now()),telegram_file_id='' WHERE id IN (SELECT id FROM file_purge_queue WHERE kind='receipt');
 UPDATE documents SET state='deleted',expires_at=least(coalesce(expires_at,now()),now()) WHERE id IN (SELECT id FROM file_purge_queue WHERE kind IN ('personal','shared'));
 UPDATE document_quota q SET bytes=(SELECT coalesce(sum(size_bytes),0) FROM documents d WHERE d.workspace_id=q.workspace_id AND d.state<>'deleted') WHERE q.workspace_id IN(SELECT id FROM workspaces WHERE kind IN ('personal','shared'));
 UPDATE telegram_updates SET response=jsonb_build_object('text','Событие уже обработано. /history — сохранённые операции.'),workspace_id=NULL WHERE created_at<now()-interval '30 days' AND workspace_id IS NOT NULL;
 UPDATE voice_jobs SET transcript=NULL,result=NULL,reply=NULL WHERE created_at<now()-interval '1 day' AND state<>'processing';
 UPDATE ai_jobs SET request='{}' WHERE created_at<now()-interval '30 days' AND state IN ('succeeded','failed','cancelled');
 UPDATE receipt_batches SET result=NULL,reply=NULL,categories_snapshot=NULL WHERE created_at<now()-interval '30 days' AND state NOT IN ('collecting','processing');
 DELETE FROM admin_login_codes WHERE expires_at<now();
 UPDATE diagnostic_grants SET payload='{}',revoked=true WHERE expires_at<now() AND NOT revoked;
 FOREACH t IN ARRAY tables LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
END $$;
CREATE FUNCTION plan_retention_notices() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE r record;b retention_batches;old_mode text:=current_setting('balans.notification_worker',true);t text;
BEGIN
 PERFORM pg_advisory_xact_lock(23001);
 FOREACH t IN ARRAY ARRAY['receipt_files','documents','retention_batches','workspaces','notification_outbox'] LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 FOR r IN SELECT f.workspace_id,w.owner_user_id,w.owner_telegram_id,date_trunc('day',f.expires_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'+interval '1 day' AS deadline,jsonb_agg(jsonb_build_object('id',f.id,'kind',f.kind,'size',f.size_bytes,'mime',f.mime_type,'sha',f.sha256)) AS files
 FROM (SELECT id,workspace_id,expires_at,size_bytes,mime_type,sha256,'receipt' AS kind FROM receipt_files WHERE telegram_file_id<>'' AND retention_warned_at IS NULL AND expires_at<date_trunc('day',now()+interval '7 days')+interval '1 day'
 UNION ALL SELECT id,workspace_id,expires_at,size_bytes,mime_type,sha256,CASE WHEN permanent THEN 'shared' ELSE 'personal' END FROM documents WHERE state<>'deleted' AND retention_warned_at IS NULL AND expires_at<date_trunc('day',now()+interval '7 days')+interval '1 day') f JOIN workspaces w ON w.id=f.workspace_id
 GROUP BY f.workspace_id,w.owner_user_id,w.owner_telegram_id,date_trunc('day',f.expires_at AT TIME ZONE 'UTC') LOOP
  INSERT INTO retention_batches(workspace_id,user_id,deadline,files) VALUES(r.workspace_id,r.owner_user_id,r.deadline,r.files) ON CONFLICT(workspace_id,deadline) DO NOTHING;
  SELECT * INTO b FROM retention_batches WHERE workspace_id=r.workspace_id AND deadline=r.deadline;
  IF b.extended_at IS NOT NULL THEN CONTINUE;END IF;
  INSERT INTO notification_outbox(workspace_id,user_id,telegram_user_id,kind,entity_kind,entity_id,message,event_key,expires_at)
  VALUES(r.workspace_id,r.owner_user_id,r.owner_telegram_id,'retention','retention',b.id,
   '📎 Срок хранения файлов заканчивается. Файлов: '||(SELECT count(DISTINCT x->>'sha') FROM jsonb_array_elements(b.files) x)||'. Дата: '||to_char(b.deadline,'DD.MM.YYYY')||'. Можно продлить хранение на 90 дней за дополнительную плату или скачать архив. Удалятся только файлы — операции и отчёты останутся. Без ответа файлы будут удалены в срок, но не раньше чем через 7 дней после этого уведомления.',
   'retention:'||b.id||':7',now()+interval '180 days') ON CONFLICT(user_id,event_key) DO NOTHING;

 END LOOP;
 FOR b IN SELECT * FROM retention_batches WHERE notice_sent_at IS NOT NULL AND extended_at IS NULL AND greatest(deadline,notice_sent_at+interval '7 days')<=now()+interval '1 day' LOOP
  INSERT INTO notification_outbox(workspace_id,user_id,telegram_user_id,kind,entity_kind,entity_id,message,event_key,expires_at)
  SELECT b.workspace_id,b.user_id,w.owner_telegram_id,'retention','retention',b.id,'📎 Напоминание: срок хранения '||(SELECT count(DISTINCT x->>'sha') FROM jsonb_array_elements(b.files) x)||' файлов истекает. Продлите срок или скачайте архив. Операции и отчёты останутся.','retention:'||b.id||':1',now()+interval '7 days' FROM workspaces w WHERE w.id=b.workspace_id
  ON CONFLICT(user_id,event_key) DO NOTHING;
 END LOOP;
 FOREACH t IN ARRAY ARRAY['receipt_files','documents','retention_batches','workspaces','notification_outbox'] LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
 PERFORM set_config('balans.notification_worker',coalesce(old_mode,''),true);
END $$;
CREATE FUNCTION mark_retention_notice(batch uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE b retention_batches;f jsonb;t text;
BEGIN
 SELECT * INTO b FROM retention_batches WHERE id=batch AND user_id=actor_user_id() FOR UPDATE;
 IF NOT FOUND OR b.notice_sent_at IS NOT NULL THEN RETURN;END IF;
 UPDATE retention_batches SET notice_sent_at=now() WHERE id=batch;
 FOREACH t IN ARRAY ARRAY['receipt_files','documents'] LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 FOR f IN SELECT * FROM jsonb_array_elements(b.files) LOOP
  IF f->>'kind'='receipt' THEN UPDATE receipt_files SET retention_warned_at=now(),expires_at=greatest(expires_at,b.deadline,now()+interval '7 days') WHERE id=(f->>'id')::uuid AND workspace_id=b.workspace_id;
  ELSE UPDATE documents SET retention_warned_at=now(),expires_at=greatest(expires_at,b.deadline,now()+interval '7 days') WHERE id=(f->>'id')::uuid AND workspace_id=b.workspace_id;END IF;
 END LOOP;
 FOREACH t IN ARRAY ARRAY['receipt_files','documents'] LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
END $$;
CREATE TABLE addon_orders(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,workspace_id uuid NOT NULL REFERENCES workspaces,product text NOT NULL CHECK(product IN ('storage','text','image','voice','analysis','upgrade')),stars integer NOT NULL CHECK(stars>0),units integer NOT NULL CHECK(units>0),batch_id uuid REFERENCES retention_batches,demo_generation uuid NOT NULL,period_start timestamptz,period_end timestamptz,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','paid')),created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '15 minutes');
ALTER TABLE addon_orders ENABLE ROW LEVEL SECURITY;ALTER TABLE addon_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON addon_orders USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());
ALTER FUNCTION billing_access(uuid) RENAME TO billing_access_base;
CREATE FUNCTION billing_access(space uuid DEFAULT current_workspace()) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE a jsonb:=billing_access_base(space);q jsonb; k text;n integer;
BEGIN
 IF NOT coalesce((a->>'demo')::boolean,false) OR a->>'status' NOT IN ('trial','active') THEN RETURN a;END IF;
 q:=a->'quotas';
 FOREACH k IN ARRAY ARRAY['text','image','voice','analysis'] LOOP
  SELECT coalesce(sum(CASE WHEN o.product='upgrade' THEN (a->'quotas'->>k)::integer ELSE o.units END),0) INTO n FROM addon_orders o JOIN billing_demo d ON d.user_id=o.user_id AND d.generation=o.demo_generation
  WHERE o.user_id=(a->>'sponsor')::uuid AND o.state='paid' AND o.period_start=(a->>'period_start')::timestamptz AND o.period_end>now() AND o.product IN (k,'upgrade');
  q:=jsonb_set(q,ARRAY[k],to_jsonb((q->>k)::integer+n));
 END LOOP;
 RETURN jsonb_set(a,ARRAY['quotas'],q);
END $$;
CREATE FUNCTION confirm_demo_addon(identity uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE o addon_orders;d billing_demo;b retention_batches;f jsonb;t text;
BEGIN
 SELECT * INTO o FROM addon_orders WHERE id=identity AND user_id=actor_user_id() AND workspace_id=current_workspace() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Предложение недоступно';END IF;
 IF o.state='paid' THEN RETURN false;END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(o.user_id::text,23002));
 SELECT * INTO d FROM billing_demo WHERE user_id=actor_user_id() AND allowed AND enabled AND generation=o.demo_generation;
 IF NOT FOUND OR o.expires_at<=now() THEN RAISE EXCEPTION 'Предложение устарело';END IF;
 IF o.product='storage' THEN
  SELECT * INTO b FROM retention_batches WHERE id=o.batch_id AND user_id=actor_user_id() AND workspace_id=current_workspace() AND extended_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Срок уже продлён или файлы недоступны';END IF;
  IF EXISTS(SELECT 1 FROM workspaces w JOIN user_settings s ON s.user_id=w.owner_user_id WHERE w.id=b.workspace_id AND w.kind='personal' AND NOT s.keep_personal_originals) THEN RAISE EXCEPTION 'Сначала включите хранение личных оригиналов в настройках';END IF;
  FOREACH t IN ARRAY ARRAY['receipt_files','documents','file_purge_queue'] LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
  IF EXISTS(SELECT 1 FROM file_purge_queue p JOIN jsonb_array_elements(b.files) entry ON p.id=(entry->>'id')::uuid) THEN RAISE EXCEPTION 'Удаление файлов уже началось. Продление недоступно';END IF;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(b.files) item LEFT JOIN receipt_files rf ON rf.id=(item->>'id')::uuid LEFT JOIN documents doc ON doc.id=(item->>'id')::uuid
   WHERE (item->>'kind'='receipt' AND (rf.id IS NULL OR rf.telegram_file_id='')) OR (item->>'kind'<>'receipt' AND (doc.id IS NULL OR doc.state='deleted'))) THEN RAISE EXCEPTION 'Часть файлов уже удалена. Продление этой подборки недоступно';END IF;
  FOR f IN SELECT * FROM jsonb_array_elements(b.files) LOOP
   IF f->>'kind'='receipt' THEN UPDATE receipt_files SET expires_at=greatest(expires_at,b.deadline,now())+interval '90 days',retention_warned_at=NULL WHERE id=(f->>'id')::uuid AND workspace_id=b.workspace_id;
   ELSE UPDATE documents SET expires_at=greatest(expires_at,b.deadline,now())+interval '90 days',retention_warned_at=NULL WHERE id=(f->>'id')::uuid AND workspace_id=b.workspace_id AND state<>'deleted';END IF;
  END LOOP;
  FOREACH t IN ARRAY ARRAY['receipt_files','documents','file_purge_queue'] LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
  UPDATE retention_batches SET extended_at=now() WHERE id=b.id;
  UPDATE notification_outbox SET state='cancelled' WHERE entity_kind='retention' AND entity_id=b.id AND state='pending';
 ELSE
  IF o.period_end<=now() OR o.period_start IS DISTINCT FROM (billing_access_base()->>'period_start')::timestamptz THEN RAISE EXCEPTION 'Период квот изменился';END IF;
  IF o.product='upgrade' AND EXISTS(SELECT 1 FROM addon_orders WHERE user_id=o.user_id AND product='upgrade' AND state='paid' AND period_start=o.period_start AND demo_generation=o.demo_generation) THEN RAISE EXCEPTION 'Расширенный тариф уже подключён';END IF;
 END IF;
 UPDATE addon_orders SET state='paid' WHERE id=o.id;RETURN true;
END $$;
REVOKE ALL ON FUNCTION file_expiry_default(),plan_retention_notices(),mark_retention_notice(uuid),confirm_demo_addon(uuid),billing_access(uuid),billing_access_base(uuid) FROM PUBLIC;
CREATE FUNCTION quota_notice() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE a jsonb;used bigint;maximum integer;label text;threshold text;old_mode text:=current_setting('balans.notification_worker',true);
BEGIN
 IF NEW.state<>'consumed' OR (TG_OP='UPDATE' AND OLD.state='consumed') THEN RETURN NEW;END IF;
 a:=billing_access(NEW.workspace_id);maximum:=(a->'quotas'->>NEW.kind)::integer;
 IF maximum IS NULL OR maximum<=0 THEN RETURN NEW;END IF;
 SELECT coalesce(sum(units),0) INTO used FROM quota_reservations WHERE sponsor_id=NEW.sponsor_id AND kind=NEW.kind AND period_start=NEW.period_start AND state IN ('reserved','consumed');
 IF used<maximum*0.8 THEN RETURN NEW;END IF;
 threshold:=CASE WHEN used>=maximum THEN '100' ELSE '80' END;
 label:=CASE NEW.kind WHEN 'text' THEN 'ИИ-категоризации' WHEN 'image' THEN 'Распознавание файлов' WHEN 'voice' THEN 'Голосовой ввод (секунды)' ELSE 'ИИ-анализ отчётов' END;
 PERFORM set_config('balans.notification_worker','on',true);
 INSERT INTO notification_outbox(workspace_id,user_id,telegram_user_id,kind,entity_kind,message,event_key,expires_at)
 SELECT NEW.workspace_id,NEW.sponsor_id,w.owner_telegram_id,'quota',NEW.kind,
  CASE WHEN threshold='100' THEN '🤖 Лимит ИИ исчерпан.' ELSE '🤖 Осталось не больше 20% лимита ИИ.' END||E'\n'||label||': '||used||' из '||maximum||E'.\nОбновление лимита: '||to_char((a->>'period_end')::timestamptz,'DD.MM.YYYY')||'. Можно добавить пакет или выбрать расширенный тариф. Ручной ввод остаётся доступен.',
  'quota:'||NEW.sponsor_id||':'||NEW.kind||':'||NEW.period_start||':'||maximum||':'||threshold,(a->>'period_end')::timestamptz
 FROM workspaces w WHERE w.id=NEW.workspace_id ON CONFLICT(user_id,event_key) DO NOTHING;
 PERFORM set_config('balans.notification_worker',coalesce(old_mode,''),true);RETURN NEW;
END $$;
CREATE TRIGGER quota_notice AFTER INSERT OR UPDATE ON quota_reservations FOR EACH ROW EXECUTE FUNCTION quota_notice();
REVOKE ALL ON FUNCTION quota_notice() FROM PUBLIC;

CREATE OR REPLACE FUNCTION track_job_quota() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE k text;quantity integer:=1;old_mode text:=current_setting('balans.billing_worker',true);
BEGIN
 k:=CASE TG_TABLE_NAME WHEN 'voice_jobs' THEN 'voice' WHEN 'receipt_batches' THEN 'image' WHEN 'report_jobs' THEN 'analysis' ELSE 'text' END;
 IF TG_TABLE_NAME='report_jobs' THEN IF NEW.kind<>'analysis' THEN RETURN NEW;END IF;END IF;
 IF TG_TABLE_NAME='voice_jobs' THEN quantity:=NEW.duration_seconds;END IF;
 IF TG_TABLE_NAME='receipt_batches' THEN SELECT greatest(count(*),1)::integer INTO quantity FROM receipt_files WHERE batch_id=NEW.id;END IF;
 IF NEW.state IN ('queued','running','pending','processing') THEN PERFORM reserve_quota(NEW.id,NEW.workspace_id,NEW.author_user_id,k,quantity);END IF;
 PERFORM set_config('balans.billing_worker','on',true);
 IF NEW.state IN ('failed','cancelled') THEN UPDATE quota_reservations SET state='released' WHERE job_id=NEW.id AND kind=k AND state='reserved';
 ELSIF NEW.state IN ('ready','succeeded') THEN UPDATE quota_reservations SET state='consumed' WHERE job_id=NEW.id AND kind=k AND state='reserved';END IF;
 PERFORM set_config('balans.billing_worker',coalesce(old_mode,''),true);RETURN NEW;
END $$;
