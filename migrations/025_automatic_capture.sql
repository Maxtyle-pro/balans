SET search_path=balans,pg_catalog;
ALTER TABLE user_settings ADD COLUMN automatic_capture boolean NOT NULL DEFAULT true;
ALTER TABLE operation_drafts ADD COLUMN automatic_capture boolean NOT NULL DEFAULT false,ADD COLUMN capture_warnings text[] NOT NULL DEFAULT '{}';
ALTER TABLE operation_revisions ADD COLUMN capture_warnings text[] NOT NULL DEFAULT '{}';
ALTER TABLE input_batches ADD COLUMN capture_reply jsonb;
CREATE FUNCTION uncategorized_category(space uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE identity uuid;
BEGIN
 IF space IS DISTINCT FROM current_workspace() THEN RAISE EXCEPTION 'Бюджет недоступен';END IF;
 INSERT INTO categories(workspace_id,name) VALUES(space,'Без категории') ON CONFLICT(workspace_id,name) DO UPDATE SET archived=false RETURNING id INTO identity;
 RETURN identity;
END $$;
CREATE FUNCTION revision_capture_warnings() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 SELECT d.capture_warnings INTO NEW.capture_warnings FROM operation_drafts d WHERE d.workspace_id=NEW.workspace_id AND d.author_user_id=actor_user_id() AND d.state='pending' AND (d.edit_operation_id=NEW.operation_id OR d.id=(SELECT source_draft_id FROM operations WHERE id=NEW.operation_id)) ORDER BY d.created_at DESC LIMIT 1;
 NEW.capture_warnings:=coalesce(NEW.capture_warnings,'{}');RETURN NEW;
END $$;
CREATE TRIGGER revision_capture_warnings BEFORE INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION revision_capture_warnings();
REVOKE ALL ON FUNCTION uncategorized_category(uuid),revision_capture_warnings() FROM PUBLIC;
