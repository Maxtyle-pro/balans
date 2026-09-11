SET search_path=balans,pg_catalog;
ALTER TABLE categories ADD COLUMN archived boolean NOT NULL DEFAULT false;
CREATE TABLE category_events(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,category_id uuid NOT NULL REFERENCES categories,action text NOT NULL,old_name text,new_name text,created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE category_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE category_events FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON category_events USING(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces)) WITH CHECK(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces));
CREATE TABLE input_batches(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,account_id uuid NOT NULL,timezone_snapshot text NOT NULL,source_sent_at timestamptz NOT NULL,items jsonb NOT NULL,state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','done','cancelled')),created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id));
CREATE UNIQUE INDEX one_input_batch ON input_batches(author_user_id) WHERE state='active';
ALTER TABLE input_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE input_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON input_batches USING(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces)) WITH CHECK(author_user_id=actor_user_id() AND workspace_id IN(SELECT id FROM workspaces));
CREATE FUNCTION manage_category(action_name text,category_name text,new_name text DEFAULT NULL) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w uuid; cat categories; result uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 SELECT id INTO w FROM workspaces WHERE owner_user_id=actor_user_id() AND kind='personal';
 IF w IS NULL THEN RAISE EXCEPTION 'Бюджет недоступен'; END IF;
 IF length(trim(category_name)) NOT BETWEEN 1 AND 60 OR (new_name IS NOT NULL AND length(trim(new_name)) NOT BETWEEN 1 AND 60) THEN RAISE EXCEPTION 'Название: 1–60 символов'; END IF;
 SELECT * INTO cat FROM categories WHERE workspace_id=w AND lower(name)=lower(trim(category_name));
 IF action_name='add' THEN
  IF cat.id IS NOT NULL THEN RETURN cat.id; END IF;
  IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)>=30 THEN RAISE EXCEPTION 'Лимит: 30 активных категорий'; END IF;
  INSERT INTO categories(workspace_id,name) VALUES(w,trim(category_name)) RETURNING id INTO result;
 ELSIF action_name IN ('rename','archive','restore') THEN
  IF cat.id IS NULL THEN RAISE EXCEPTION 'Категория не найдена'; END IF;
  result:=cat.id;
  IF action_name='rename' THEN
   IF new_name IS NULL OR EXISTS(SELECT 1 FROM categories WHERE workspace_id=w AND lower(name)=lower(trim(new_name)) AND id<>cat.id) THEN RAISE EXCEPTION 'Новое название отсутствует или уже занято'; END IF;
   UPDATE categories SET name=trim(new_name) WHERE id=cat.id;
  ELSIF action_name='archive' THEN
   IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)<=1 THEN RAISE EXCEPTION 'Оставьте хотя бы одну активную категорию'; END IF;
   UPDATE categories SET archived=true WHERE id=cat.id;
   UPDATE category_rules SET enabled=false WHERE category_id=cat.id;
  ELSE
   IF (SELECT count(*) FROM categories WHERE workspace_id=w AND NOT archived)>=30 AND cat.archived THEN RAISE EXCEPTION 'Лимит: 30 активных категорий'; END IF;
   UPDATE categories SET archived=false WHERE id=cat.id;
  END IF;
 ELSE RAISE EXCEPTION 'Неизвестное действие'; END IF;
 INSERT INTO category_events(workspace_id,author_user_id,category_id,action,old_name,new_name) VALUES(w,actor_user_id(),result,action_name,cat.name,coalesce(new_name,category_name));
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION manage_category(text,text,text) FROM PUBLIC;
