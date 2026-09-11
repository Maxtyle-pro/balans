SET search_path=balans,pg_catalog;
CREATE TABLE reports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,
 author_user_id uuid NOT NULL REFERENCES users,snapshot jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '7 days',
 UNIQUE(workspace_id,id)
);
CREATE TABLE sheets_connections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),author_user_id uuid NOT NULL REFERENCES users,
 spreadsheet_id text NOT NULL,challenge text NOT NULL,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','revoked')),
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes',
 UNIQUE(author_user_id,id)
);
CREATE UNIQUE INDEX one_sheets_connection ON sheets_connections(author_user_id) WHERE state IN ('pending','active');
CREATE TABLE report_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,
 report_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('analysis','sheets')),state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','ready','failed')),
 connection_id uuid,tab_id integer CHECK(tab_id>0),reply jsonb,error_code text,model text,prompt_version text,
 created_at timestamptz NOT NULL DEFAULT now(),lease_until timestamptz,
 FOREIGN KEY(workspace_id,report_id) REFERENCES reports(workspace_id,id),
 FOREIGN KEY(author_user_id,connection_id) REFERENCES sheets_connections(author_user_id,id),
 UNIQUE(report_id,kind)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['reports','report_jobs'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY own ON %I USING(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces)) WITH CHECK(author_user_id=balans.actor_user_id() AND workspace_id IN (SELECT id FROM balans.workspaces))',t);
 END LOOP;
END $$;
ALTER TABLE sheets_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE sheets_connections FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON sheets_connections USING(author_user_id=actor_user_id()) WITH CHECK(author_user_id=actor_user_id());
