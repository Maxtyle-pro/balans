SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN ai_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN ai_consent_version text, ADD COLUMN ai_consented_at timestamptz;
ALTER TABLE operation_drafts ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK(version>0),
  ADD COLUMN flow text NOT NULL DEFAULT 'manual' CHECK(flow IN ('manual','auto')),
  ADD COLUMN category_source text NOT NULL DEFAULT 'manual' CHECK(category_source IN ('manual','rule','ai','fallback')),
  ADD COLUMN category_confidence numeric(4,3) CHECK(category_confidence BETWEEN 0 AND 1);
ALTER TABLE operation_drafts DROP CONSTRAINT operation_drafts_step_check;
ALTER TABLE operation_drafts ADD CHECK(step IN ('amount','category','description','date','confirm','ai_pending','category_review'));
CREATE TABLE category_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  membership_id uuid NOT NULL, author_user_id uuid NOT NULL REFERENCES users,
  match_kind text NOT NULL CHECK(match_kind IN ('description','keyword')),
  pattern text NOT NULL CHECK(length(pattern) BETWEEN 1 AND 500), category_id uuid NOT NULL,
  enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,author_user_id,match_kind,pattern),
  FOREIGN KEY(workspace_id,membership_id) REFERENCES memberships(workspace_id,id),
  FOREIGN KEY(workspace_id,category_id) REFERENCES categories(workspace_id,id)
);
CREATE TABLE category_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  author_user_id uuid NOT NULL REFERENCES users, draft_id uuid, operation_id uuid,
  old_category_id uuid, new_category_id uuid NOT NULL, description text NOT NULL,
  expected_version integer, expected_revision_id uuid, learned_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK((draft_id IS NOT NULL AND operation_id IS NULL AND expected_version IS NOT NULL AND expected_revision_id IS NULL)
     OR (operation_id IS NOT NULL AND draft_id IS NULL AND expected_version IS NULL AND expected_revision_id IS NOT NULL)),
  FOREIGN KEY(workspace_id,draft_id) REFERENCES operation_drafts(workspace_id,id),
  FOREIGN KEY(workspace_id,operation_id) REFERENCES operations(workspace_id,id),
  FOREIGN KEY(workspace_id,old_category_id) REFERENCES categories(workspace_id,id),
  FOREIGN KEY(workspace_id,new_category_id) REFERENCES categories(workspace_id,id),
  FOREIGN KEY(workspace_id,operation_id,expected_revision_id) REFERENCES operation_revisions(workspace_id,operation_id,id)
);
CREATE TABLE category_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  author_user_id uuid NOT NULL REFERENCES users, operation_id uuid NOT NULL, expected_revision_id uuid NOT NULL,
  category_id uuid NOT NULL, consumed_at timestamptz, expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
  FOREIGN KEY(workspace_id,operation_id,expected_revision_id) REFERENCES operation_revisions(workspace_id,operation_id,id),
  FOREIGN KEY(workspace_id,category_id) REFERENCES categories(workspace_id,id)
);
CREATE TABLE ai_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  author_user_id uuid NOT NULL REFERENCES users, draft_id uuid NOT NULL, draft_version integer NOT NULL,
  state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
  provider text NOT NULL DEFAULT 'openai', model text NOT NULL, prompt_version text NOT NULL,
  request jsonb NOT NULL, response_id text, category_id uuid, confidence numeric(4,3),
  input_tokens integer, output_tokens integer, error_code text,
  started_at timestamptz, finished_at timestamptz, lease_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(draft_id,draft_version),
  FOREIGN KEY(workspace_id,draft_id) REFERENCES operation_drafts(workspace_id,id),
  FOREIGN KEY(workspace_id,category_id) REFERENCES categories(workspace_id,id),
  CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
);
CREATE INDEX ai_jobs_owner_idx ON ai_jobs(author_user_id,state);
CREATE INDEX category_rules_owner_idx ON category_rules(workspace_id,author_user_id) WHERE enabled;
CREATE INDEX category_feedback_owner_idx ON category_feedback(author_user_id,created_at DESC);
CREATE INDEX category_actions_owner_idx ON category_actions(author_user_id);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['category_rules','category_feedback','category_actions','ai_jobs'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY own ON %I USING(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces)) WITH CHECK(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces))',t);
  END LOOP;
END $$;
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CHECK(action IN ('expense_created','category_changed'));

-- Category-only corrections keep the monetary journal unchanged.
CREATE FUNCTION change_category(action_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path=balans,pg_temp AS $$
DECLARE a category_actions; o operations; r operation_revisions; new_id uuid:=gen_random_uuid(); f uuid;
BEGIN
  SELECT * INTO a FROM category_actions WHERE id=action_id AND author_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR a.expires_at<=now() OR a.consumed_at IS NOT NULL THEN RAISE EXCEPTION 'Action unavailable'; END IF;
  SELECT * INTO o FROM operations WHERE id=a.operation_id AND created_by_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND OR o.current_revision_id<>a.expected_revision_id THEN RAISE EXCEPTION 'Revision conflict'; END IF;
  SELECT * INTO r FROM operation_revisions WHERE id=o.current_revision_id;
  UPDATE category_actions SET consumed_at=now() WHERE id=a.id;
  IF r.category_id=a.category_id THEN RETURN NULL; END IF;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,revision_no,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot,source_kind)
    VALUES(new_id,r.workspace_id,r.operation_id,r.revision_no+1,r.account_id,a.category_id,r.amount,r.currency,r.description,r.occurred_on,r.timezone_snapshot,r.source_kind);
  UPDATE operations SET current_revision_id=new_id WHERE id=o.id;
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(o.workspace_id,actor_user_id(),o.id,'category_changed');
  INSERT INTO category_feedback(workspace_id,author_user_id,operation_id,old_category_id,new_category_id,description,expected_revision_id)
    VALUES(o.workspace_id,actor_user_id(),o.id,r.category_id,a.category_id,r.description,new_id) RETURNING id INTO f;
  RETURN f;
END $$;
REVOKE ALL ON FUNCTION change_category(uuid) FROM PUBLIC;
ALTER TABLE ai_jobs ADD COLUMN reply jsonb;
