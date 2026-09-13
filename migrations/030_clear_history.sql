SET search_path TO balans, public;
CREATE TABLE history_clear_requests(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES users,workspace_id uuid NOT NULL,expires_at timestamptz NOT NULL DEFAULT now()+interval '10 minutes',completed_at timestamptz);
ALTER TABLE history_clear_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE history_clear_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON history_clear_requests USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());
CREATE FUNCTION clear_personal_history(identity uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE person uuid:=actor_user_id();request history_clear_requests;old_space workspaces;fresh uuid;member uuid;account uuid;forced text[];t text;
BEGIN
 PERFORM pg_advisory_xact_lock(16001001);
 PERFORM pg_advisory_xact_lock(actor_telegram_id());
 SELECT * INTO request FROM history_clear_requests WHERE id=identity AND user_id=person FOR UPDATE;
 IF NOT FOUND OR request.expires_at<now() THEN RAISE EXCEPTION 'Подтверждение устарело. Откройте очистку истории заново.';END IF;
 IF request.completed_at IS NOT NULL THEN RETURN false;END IF;
 SELECT * INTO old_space FROM workspaces WHERE id=request.workspace_id AND owner_user_id=person AND kind='personal';
 IF NOT FOUND THEN RAISE EXCEPTION 'Личная история недоступна.';END IF;
 SELECT array_agg(relname) INTO forced FROM pg_class WHERE relnamespace='balans'::regnamespace AND relkind='r' AND relforcerowsecurity;
 FOREACH t IN ARRAY forced LOOP EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);END LOOP;
 -- Build the replacement before moving billing records; never reset paid usage.
 INSERT INTO workspaces(owner_user_id,kind,name,base_currency,timezone) VALUES(person,'shared',old_space.name,old_space.base_currency,old_space.timezone) RETURNING id INTO fresh;
 INSERT INTO memberships(workspace_id,user_id,owner_user_id,telegram_user_id) VALUES(fresh,person,person,actor_telegram_id()) RETURNING id INTO member;
 INSERT INTO accounts(workspace_id,responsible_membership_id,currency) VALUES(fresh,member,old_space.base_currency) RETURNING id INTO account;
 INSERT INTO categories(workspace_id,name,archived) SELECT fresh,name,archived FROM categories WHERE workspace_id=old_space.id;
 INSERT INTO category_rules(workspace_id,membership_id,author_user_id,match_kind,pattern,category_id,enabled)
 SELECT fresh,member,r.author_user_id,r.match_kind,r.pattern,n.id,r.enabled FROM category_rules r JOIN categories o ON o.id=r.category_id JOIN categories n ON n.workspace_id=fresh AND n.name=o.name WHERE r.workspace_id=old_space.id;
 UPDATE quota_reservations SET workspace_id=fresh WHERE workspace_id=old_space.id;
 UPDATE retention_batches SET workspace_id=fresh,files='[]' WHERE workspace_id=old_space.id;
 UPDATE addon_orders SET workspace_id=fresh WHERE workspace_id=old_space.id;
 INSERT INTO file_purge_queue(id,kind) SELECT id,'receipt' FROM receipt_files WHERE workspace_id=old_space.id ON CONFLICT DO NOTHING;
 INSERT INTO file_purge_queue(id,kind) SELECT id,CASE WHEN permanent THEN 'shared' ELSE 'personal' END FROM documents WHERE workspace_id=old_space.id ON CONFLICT DO NOTHING;
 UPDATE user_settings SET selected_workspace_id=fresh,default_account_id=account WHERE user_id=person;
 DELETE FROM telegram_updates WHERE user_id=person AND workspace_id=old_space.id;
 DELETE FROM workspaces WHERE id=old_space.id;
 UPDATE workspaces SET kind='personal' WHERE id=fresh;
 UPDATE history_clear_requests SET completed_at=now() WHERE id=identity;
 SET CONSTRAINTS ALL IMMEDIATE;
 FOREACH t IN ARRAY forced LOOP EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);END LOOP;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION clear_personal_history(uuid) FROM PUBLIC;
