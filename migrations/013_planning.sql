SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN budget_start_day integer NOT NULL DEFAULT 1 CHECK(budget_start_day BETWEEN 1 AND 28);
ALTER TABLE users ADD UNIQUE(id,telegram_user_id);
ALTER TABLE workspaces ADD COLUMN owner_telegram_id bigint;
ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;ALTER TABLE memberships NO FORCE ROW LEVEL SECURITY;
UPDATE workspaces w SET owner_telegram_id=m.telegram_user_id FROM memberships m WHERE m.workspace_id=w.id AND m.role='owner';
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces ALTER COLUMN owner_telegram_id SET NOT NULL,ALTER COLUMN owner_telegram_id SET DEFAULT actor_telegram_id();
CREATE TABLE spending_budgets(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,category_id uuid REFERENCES categories,amount numeric(20,2) NOT NULL CHECK(amount>0 AND amount<=999999999999.99),active boolean NOT NULL DEFAULT true,created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX budget_per_category ON spending_budgets(workspace_id,author_user_id,category_id) NULLS NOT DISTINCT;
CREATE TABLE budget_alerts(workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,budget_id uuid NOT NULL REFERENCES spending_budgets,period_start date NOT NULL,threshold integer NOT NULL CHECK(threshold IN (80,100)),created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(budget_id,period_start,threshold));
CREATE TABLE notification_preferences(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,enabled boolean NOT NULL DEFAULT false,budget_alerts boolean NOT NULL DEFAULT true,reminder boolean NOT NULL DEFAULT false,weekly boolean NOT NULL DEFAULT false,shared_mode text NOT NULL DEFAULT 'off' CHECK(shared_mode IN ('off','instant','daily')),event_types text[] NOT NULL DEFAULT ARRAY['expense','funds','review'],quiet_start integer DEFAULT 1320 CHECK(quiet_start BETWEEN 0 AND 1439),quiet_end integer DEFAULT 540 CHECK(quiet_end BETWEEN 0 AND 1439),send_minute integer NOT NULL DEFAULT 1140 CHECK(send_minute BETWEEN 0 AND 1439),weekday integer NOT NULL DEFAULT 0 CHECK(weekday BETWEEN 0 AND 6),blocked boolean NOT NULL DEFAULT false,enabled_at timestamptz NOT NULL DEFAULT now(),next_check_at timestamptz NOT NULL DEFAULT now(),UNIQUE(workspace_id,user_id),FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
CREATE TABLE notification_events(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,event_type text NOT NULL,entity_kind text NOT NULL,entity_id uuid,event_key text NOT NULL,processed boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,event_key),FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
CREATE TABLE notification_outbox(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,user_id uuid NOT NULL REFERENCES users,telegram_user_id bigint NOT NULL,kind text NOT NULL,entity_kind text,entity_id uuid,message text NOT NULL CHECK(length(message)<=3500),event_key text NOT NULL,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent','cancelled','uncertain')),next_attempt_at timestamptz NOT NULL DEFAULT now(),lease_until timestamptz,attempts integer NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '7 days',UNIQUE(user_id,event_key),FOREIGN KEY(user_id,telegram_user_id) REFERENCES users(id,telegram_user_id));
DO $$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['spending_budgets','budget_alerts','notification_preferences','notification_events','notification_outbox'] LOOP EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;END $$;
CREATE POLICY own ON spending_budgets USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE POLICY own ON budget_alerts USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE POLICY own ON notification_preferences USING(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on') WITH CHECK(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on');
CREATE POLICY own ON notification_events USING(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on') WITH CHECK(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on');
CREATE POLICY own ON notification_outbox USING(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on') WITH CHECK(user_id=actor_user_id() OR current_setting('balans.notification_worker',true)='on');
CREATE FUNCTION enqueue_notification_event(space uuid,recipient uuid,telegram bigint,event_type_name text,entity_type text,entity uuid,key text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE old_mode text:=current_setting('balans.notification_worker',true);
BEGIN
 IF recipient=actor_user_id() THEN RETURN; END IF;
 PERFORM set_config('balans.notification_worker','on',true);
 IF EXISTS(SELECT 1 FROM notification_preferences WHERE workspace_id=space AND user_id=recipient AND enabled AND NOT blocked AND shared_mode<>'off' AND event_type_name=ANY(event_types)) THEN
 INSERT INTO notification_events(workspace_id,user_id,telegram_user_id,event_type,entity_kind,entity_id,event_key) VALUES(space,recipient,telegram,event_type_name,entity_type,entity,key) ON CONFLICT DO NOTHING;
 END IF;
 PERFORM set_config('balans.notification_worker',coalesce(old_mode,''),true);
END $$;
CREATE FUNCTION notify_operation_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces;
BEGIN SELECT * INTO w FROM workspaces WHERE id=NEW.workspace_id AND kind='shared';IF FOUND THEN PERFORM enqueue_notification_event(w.id,w.owner_user_id,w.owner_telegram_id,'expense','operation',NEW.operation_id,'revision:'||NEW.id::text);END IF;RETURN NEW;END $$;
CREATE TRIGGER operation_notification AFTER INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION notify_operation_revision();
CREATE FUNCTION notify_fund_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces; f fund_transfers; cl fund_claims; recipient uuid; telegram bigint;
BEGIN
 IF NEW.action IN ('planned') THEN RETURN NEW; END IF;
 SELECT * INTO w FROM workspaces WHERE id=NEW.workspace_id;
 IF NEW.transfer_id IS NOT NULL THEN
  SELECT * INTO f FROM fund_transfers WHERE id=NEW.transfer_id;
  IF actor_user_id()=f.sender_user_id THEN recipient:=f.recipient_user_id;telegram:=f.recipient_telegram_id;ELSE recipient:=f.sender_user_id;telegram:=f.sender_telegram_id;END IF;
  PERFORM enqueue_notification_event(w.id,recipient,telegram,'funds','transfer',f.id,'fund:'||NEW.id::text);
 ELSIF NEW.claim_id IS NOT NULL THEN
  SELECT * INTO cl FROM fund_claims WHERE id=NEW.claim_id;
  IF actor_user_id()=w.owner_user_id THEN recipient:=cl.author_user_id;telegram:=cl.author_telegram_id;ELSE recipient:=w.owner_user_id;telegram:=w.owner_telegram_id;END IF;
  PERFORM enqueue_notification_event(w.id,recipient,telegram,'funds','claim',cl.id,'claim:'||NEW.id::text);
 END IF; RETURN NEW;
END $$;
CREATE TRIGGER fund_notification AFTER INSERT ON fund_audit FOR EACH ROW EXECUTE FUNCTION notify_fund_change();
CREATE FUNCTION notify_document_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w workspaces; ds document_sets; recipient uuid; telegram bigint;
BEGIN
 SELECT * INTO w FROM workspaces WHERE id=NEW.workspace_id AND kind='shared';IF NOT FOUND OR NEW.set_id IS NULL OR NEW.action='revision_changed' THEN RETURN NEW; END IF;
 SELECT * INTO ds FROM document_sets WHERE id=NEW.set_id;
 IF actor_user_id()=w.owner_user_id THEN
  recipient:=ds.author_user_id;SELECT telegram_user_id INTO telegram FROM memberships WHERE workspace_id=w.id AND user_id=recipient;
 ELSE recipient:=w.owner_user_id;telegram:=w.owner_telegram_id; END IF;
 PERFORM enqueue_notification_event(w.id,recipient,telegram,'review',ds.entity_kind,ds.id,'review:'||NEW.id::text);RETURN NEW;
END $$;
CREATE TRIGGER review_notification AFTER INSERT ON review_audit FOR EACH ROW EXECUTE FUNCTION notify_document_review();
REVOKE ALL ON FUNCTION enqueue_notification_event(uuid,uuid,bigint,text,text,uuid,text),notify_operation_revision(),notify_fund_change(),notify_document_review() FROM PUBLIC;
CREATE INDEX notification_due ON notification_outbox(next_attempt_at) WHERE state='pending';
CREATE INDEX notification_event_recipient ON notification_events(user_id,workspace_id,created_at) WHERE NOT processed;
