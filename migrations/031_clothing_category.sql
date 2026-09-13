SET search_path TO balans, public;
ALTER TABLE categories NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;
INSERT INTO categories(workspace_id,name)
 SELECT w.id,'Одежда' FROM workspaces w WHERE NOT w.archived
 AND NOT EXISTS(SELECT 1 FROM categories c WHERE c.workspace_id=w.id AND lower(c.name)='одежда')
 AND (SELECT count(*) FROM categories c WHERE c.workspace_id=w.id AND NOT c.archived)<30;
ALTER TABLE categories FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE OR REPLACE FUNCTION bootstrap() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE identity uuid;fresh boolean;
BEGIN
 fresh:=NOT EXISTS(SELECT 1 FROM workspaces WHERE owner_user_id=actor_user_id());
 identity:=bootstrap_before_admin();
 IF fresh THEN PERFORM manage_category('add','Одежда',NULL);END IF;
 PERFORM apply_content_defaults();RETURN identity;
END $$;
