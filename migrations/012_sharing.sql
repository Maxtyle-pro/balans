SET search_path=balans,pg_catalog;
ALTER TABLE telegram_updates ADD COLUMN workspace_id uuid DEFAULT current_workspace() REFERENCES workspaces;
CREATE TABLE share_drafts(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,report_id uuid NOT NULL REFERENCES reports,options jsonb NOT NULL,parts jsonb,state text NOT NULL DEFAULT 'editing' CHECK(state IN ('editing','prepared')),version integer NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours');
ALTER TABLE share_drafts ENABLE ROW LEVEL SECURITY;ALTER TABLE share_drafts FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON share_drafts USING(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces)) WITH CHECK(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces));
DROP POLICY access ON fund_entries;
CREATE POLICY access ON fund_entries USING(workspace_id=current_workspace() AND (owns_workspace(workspace_id) OR account_id IN(SELECT id FROM accounts) OR transfer_id IN(SELECT id FROM fund_transfers))) WITH CHECK(workspace_id=current_workspace() AND actor_user_id=actor_user_id());
