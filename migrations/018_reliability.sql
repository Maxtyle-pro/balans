SET search_path=balans,pg_catalog;
ALTER TABLE telegram_inbox ADD COLUMN lease_token uuid;
-- Existing single worker can have only one processing item; serialise every future claim.
CREATE UNIQUE INDEX inbox_actor_processing ON telegram_inbox(bot_id,actor_telegram_id) WHERE state='processing' AND actor_telegram_id IS NOT NULL;
CREATE INDEX inbox_actor_order ON telegram_inbox(bot_id,actor_telegram_id,update_id) WHERE state IN ('pending','processing');
-- Actor-based RLS and bootstrap must not scan all registered users' spaces/memberships.
CREATE INDEX workspaces_by_owner ON workspaces(owner_user_id);
CREATE INDEX memberships_by_user ON memberships(user_id,status,workspace_id);
CREATE INDEX memberships_by_owner ON memberships(owner_user_id,workspace_id);
-- An unset workspace override must not cause id::text scans of every workspace.
CREATE OR REPLACE FUNCTION current_workspace() RETURNS uuid LANGUAGE plpgsql STABLE SET search_path=balans,pg_temp AS $$
DECLARE requested text:=nullif(current_setting('balans.workspace_id',true),'');target uuid;result uuid;actor uuid:=actor_user_id();
BEGIN
 IF requested IS NOT NULL THEN
  BEGIN target:=requested::uuid;EXCEPTION WHEN invalid_text_representation THEN target:=NULL;END;
  SELECT id INTO result FROM workspaces WHERE id=target;
  IF result IS NOT NULL THEN RETURN result;END IF;
 END IF;
 SELECT w.id INTO result FROM user_settings s JOIN workspaces w ON w.id=s.selected_workspace_id WHERE s.user_id=actor;
 IF result IS NOT NULL THEN RETURN result;END IF;
 SELECT id INTO result FROM workspaces WHERE owner_user_id=actor AND kind='personal';
 RETURN result;
END $$;
