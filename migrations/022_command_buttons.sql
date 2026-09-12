SET search_path=balans,pg_catalog;
CREATE TABLE ui_inputs (
 user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
 action text NOT NULL,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes'
);
ALTER TABLE ui_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ui_inputs FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON ui_inputs USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());
