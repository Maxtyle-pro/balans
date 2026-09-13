SET search_path=balans,pg_catalog;
CREATE TABLE text_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
 author_user_id uuid NOT NULL REFERENCES users,account_id uuid NOT NULL,
 source_sent_at timestamptz NOT NULL,timezone_snapshot text NOT NULL,
 request jsonb NOT NULL,result jsonb,reply jsonb,error_code text,
 model text NOT NULL,input_tokens integer,output_tokens integer,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','ready','failed','cancelled')),
 created_at timestamptz NOT NULL DEFAULT now(),lease_until timestamptz,
 UNIQUE(workspace_id,id),FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id) ON DELETE CASCADE
);
ALTER TABLE text_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE text_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON text_jobs USING(author_user_id=actor_user_id() AND workspace_id IN (SELECT id FROM workspaces)) WITH CHECK(author_user_id=actor_user_id() AND workspace_id IN (SELECT id FROM workspaces));
CREATE TRIGGER text_job_quota AFTER INSERT OR UPDATE OF state ON text_jobs FOR EACH ROW EXECUTE FUNCTION track_job_quota();
ALTER TABLE operation_drafts ADD COLUMN capture_kind_pending boolean NOT NULL DEFAULT false;
CREATE FUNCTION require_capture_kind() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM operation_drafts d JOIN operations o ON o.source_draft_id=d.id WHERE o.id=NEW.operation_id AND d.capture_kind_pending) THEN RAISE EXCEPTION 'Выберите: Доход или Начальный остаток'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_capture_kind BEFORE INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION require_capture_kind();
REVOKE ALL ON FUNCTION require_capture_kind() FROM PUBLIC;

CREATE TRIGGER privacy_text BEFORE INSERT ON text_jobs FOR EACH ROW EXECUTE FUNCTION privacy_access_guard();
CREATE FUNCTION erase_text_payloads() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 UPDATE text_jobs SET request='{}',result=NULL,reply=NULL,state='cancelled' WHERE author_user_id=OLD.user_id;
 RETURN OLD;
END $$;
CREATE TRIGGER erase_text_payloads BEFORE DELETE ON user_settings FOR EACH ROW EXECUTE FUNCTION erase_text_payloads();
ALTER FUNCTION schedule_retention() RENAME TO schedule_retention_before_text;
CREATE FUNCTION schedule_retention() RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 PERFORM schedule_retention_before_text();
 ALTER TABLE text_jobs NO FORCE ROW LEVEL SECURITY;
 UPDATE text_jobs SET request='{}',result=NULL,reply=NULL WHERE created_at<now()-interval '30 days' AND state IN ('ready','failed','cancelled');
 ALTER TABLE text_jobs FORCE ROW LEVEL SECURITY;
END $$;
REVOKE ALL ON FUNCTION erase_text_payloads(),schedule_retention_before_text(),schedule_retention() FROM PUBLIC;
CREATE TRIGGER admin_job_text AFTER INSERT OR UPDATE ON text_jobs FOR EACH ROW EXECUTE FUNCTION track_admin_job();
CREATE TRIGGER cost_text AFTER INSERT OR UPDATE OF state ON text_jobs FOR EACH ROW EXECUTE FUNCTION guard_ai_estimated_cost();
