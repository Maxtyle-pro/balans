SET search_path=balans,pg_catalog;
ALTER TABLE workspaces ADD COLUMN documents_required boolean NOT NULL DEFAULT false,ADD COLUMN document_min_amount numeric(20,2) NOT NULL DEFAULT 0 CHECK(document_min_amount>=0),ADD COLUMN document_category_id uuid,ADD COLUMN document_file_limit integer NOT NULL DEFAULT 20 CHECK(document_file_limit BETWEEN 1 AND 100),ADD COLUMN document_byte_limit bigint NOT NULL DEFAULT 524288000 CHECK(document_byte_limit BETWEEN 15728640 AND 10737418240);
CREATE TABLE closed_periods(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,start_on date NOT NULL,end_on date NOT NULL,state text NOT NULL DEFAULT 'closed' CHECK(state IN ('closed','open')),reason text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),CHECK(end_on>=start_on));
CREATE TABLE document_sets(id uuid PRIMARY KEY,workspace_id uuid NOT NULL REFERENCES workspaces,entity_kind text NOT NULL CHECK(entity_kind IN ('operation','transfer','claim')),author_user_id uuid NOT NULL REFERENCES users,viewers uuid[] NOT NULL,occurred_on date NOT NULL,amount numeric(20,2) NOT NULL,category_id uuid,operation_kind text NOT NULL,status text NOT NULL DEFAULT 'unreviewed' CHECK(status IN ('unreviewed','accepted','clarification')),request_text text,exception_reason text,financial_accepted boolean NOT NULL DEFAULT false,correction_requested boolean NOT NULL DEFAULT false,correction_approved boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE documents(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),set_id uuid NOT NULL REFERENCES document_sets,workspace_id uuid NOT NULL REFERENCES workspaces,uploaded_by uuid NOT NULL REFERENCES users,mime_type text NOT NULL CHECK(mime_type IN ('image/jpeg','image/png','application/pdf')),size_bytes integer NOT NULL CHECK(size_bytes BETWEEN 1 AND 15728640),sha256 text NOT NULL CHECK(length(sha256)=64),permanent boolean NOT NULL,source_receipt_id uuid REFERENCES receipt_files,replaces_id uuid REFERENCES documents,state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded','delete_requested','deleted')),deletion_reason text,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz,UNIQUE(set_id,source_receipt_id));
CREATE TABLE document_uploads(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,author_user_id uuid NOT NULL REFERENCES users,set_id uuid NOT NULL REFERENCES document_sets,replaces_id uuid REFERENCES documents,state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','cancelled')),expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours');
CREATE UNIQUE INDEX one_document_upload ON document_uploads(author_user_id) WHERE state='pending';
CREATE TABLE document_quota(workspace_id uuid PRIMARY KEY REFERENCES workspaces,bytes bigint NOT NULL DEFAULT 0 CHECK(bytes>=0));
CREATE TABLE review_audit(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),workspace_id uuid NOT NULL REFERENCES workspaces,set_id uuid REFERENCES document_sets,document_id uuid REFERENCES documents,actor_user_id uuid NOT NULL REFERENCES users,action text NOT NULL,reason text,created_at timestamptz NOT NULL DEFAULT now());
DO $$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['closed_periods','document_sets','documents','document_uploads','document_quota','review_audit'] LOOP EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t); END LOOP; END $$;
CREATE POLICY access ON closed_periods USING(workspace_id=current_workspace()) WITH CHECK(workspace_id=current_workspace() AND owns_workspace(workspace_id));
CREATE POLICY access ON document_sets USING(workspace_id=current_workspace() AND (actor_user_id()=ANY(viewers) OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND (actor_user_id()=ANY(viewers) OR owns_workspace(workspace_id)));
CREATE POLICY access ON documents USING(workspace_id=current_workspace() AND set_id IN(SELECT id FROM document_sets)) WITH CHECK(workspace_id=current_workspace() AND set_id IN(SELECT id FROM document_sets));
CREATE POLICY access ON document_uploads USING(workspace_id=current_workspace() AND author_user_id=actor_user_id()) WITH CHECK(workspace_id=current_workspace() AND author_user_id=actor_user_id());
CREATE POLICY access ON document_quota USING(workspace_id=current_workspace()) WITH CHECK(workspace_id=current_workspace());
CREATE POLICY access ON review_audit USING(workspace_id=current_workspace() AND (owns_workspace(workspace_id) OR set_id IN(SELECT id FROM document_sets))) WITH CHECK(workspace_id=current_workspace() AND actor_user_id=actor_user_id());
CREATE FUNCTION require_open_period(day date) RETURNS void LANGUAGE plpgsql SET search_path=balans,pg_temp AS $$
BEGIN PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001)); IF EXISTS(SELECT 1 FROM closed_periods WHERE state='closed' AND day BETWEEN start_on AND end_on) THEN RAISE EXCEPTION 'Период закрыт. Руководитель должен открыть его с указанием причины'; END IF; END $$;
CREATE FUNCTION ensure_document_set(kind_name text,identity uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE w uuid; u uuid; people uuid[]; day date; value numeric; category uuid; opkind text;
BEGIN
 IF kind_name='operation' THEN
  SELECT o.workspace_id,o.created_by_user_id,ARRAY[o.created_by_user_id],r.occurred_on,r.amount,r.category_id,o.kind INTO w,u,people,day,value,category,opkind FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=identity;
 ELSIF kind_name='transfer' THEN
  SELECT workspace_id,sender_user_id,ARRAY[sender_user_id,recipient_user_id],occurred_on,amount,NULL,'transfer' INTO w,u,people,day,value,category,opkind FROM fund_transfers WHERE id=identity;
 ELSIF kind_name='claim' THEN
  SELECT workspace_id,author_user_id,ARRAY[author_user_id],occurred_on,amount,NULL,'income' INTO w,u,people,day,value,category,opkind FROM fund_claims WHERE id=identity;
 ELSE RAISE EXCEPTION 'Неизвестный тип записи'; END IF;
 IF w IS NULL THEN RAISE EXCEPTION 'Запись недоступна'; END IF;
 INSERT INTO document_sets(id,workspace_id,entity_kind,author_user_id,viewers,occurred_on,amount,category_id,operation_kind) VALUES(identity,w,kind_name,u,people,day,value,category,opkind) ON CONFLICT(id) DO NOTHING;
 RETURN identity;
END $$;
CREATE FUNCTION add_document(identity uuid,document_id uuid,mime text,size integer,sha text,replacement uuid DEFAULT NULL,receipt uuid DEFAULT NULL) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE ds document_sets; w workspaces; expired documents;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 SELECT * INTO ds FROM document_sets WHERE id=identity FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Запись недоступна'; END IF;
 PERFORM require_open_period(ds.occurred_on);
 SELECT * INTO w FROM workspaces WHERE id=ds.workspace_id;
 IF EXISTS(SELECT 1 FROM documents WHERE id=document_id) OR (receipt IS NOT NULL AND EXISTS(SELECT 1 FROM documents WHERE set_id=identity AND source_receipt_id=receipt)) THEN RETURN; END IF;
 IF replacement IS NOT NULL AND NOT EXISTS(SELECT 1 FROM documents WHERE id=replacement AND set_id=identity AND state='active') THEN RAISE EXCEPTION 'Заменяемый документ недоступен'; END IF;
 INSERT INTO document_quota(workspace_id) VALUES(w.id) ON CONFLICT DO NOTHING;
 PERFORM 1 FROM document_quota WHERE workspace_id=w.id FOR UPDATE;
 FOR expired IN SELECT * FROM documents WHERE workspace_id=w.id AND expires_at<=now() AND state<>'deleted' FOR UPDATE LOOP
  UPDATE documents SET state='deleted' WHERE id=expired.id;
  UPDATE document_quota SET bytes=bytes-expired.size_bytes WHERE workspace_id=w.id;
  INSERT INTO review_audit(workspace_id,set_id,document_id,actor_user_id,action) VALUES(w.id,expired.set_id,expired.id,actor_user_id(),'document_expired');
 END LOOP;
 IF (SELECT count(*) FROM documents WHERE set_id=identity AND state<>'deleted')>=w.document_file_limit OR (SELECT bytes+size>w.document_byte_limit FROM document_quota WHERE workspace_id=w.id) THEN RAISE EXCEPTION 'Квота документов исчерпана. Файл не добавлен'; END IF;
 INSERT INTO documents(id,set_id,workspace_id,uploaded_by,mime_type,size_bytes,sha256,permanent,source_receipt_id,replaces_id,expires_at) VALUES(document_id,identity,w.id,actor_user_id(),mime,size,sha,w.kind='shared',receipt,replacement,CASE WHEN w.kind='personal' THEN now()+interval '30 days' END);
 UPDATE document_quota SET bytes=bytes+size WHERE workspace_id=w.id;
 IF replacement IS NOT NULL THEN UPDATE documents SET state='superseded' WHERE id=replacement; END IF;
 UPDATE document_sets SET status='unreviewed',exception_reason=NULL WHERE id=identity;
 INSERT INTO review_audit(workspace_id,set_id,document_id,actor_user_id,action) VALUES(w.id,identity,document_id,actor_user_id(),CASE WHEN replacement IS NULL THEN 'document_added' ELSE 'document_replaced' END);
END $$;
CREATE FUNCTION review_record(identity uuid,action_name text,note text DEFAULT NULL) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE ds document_sets; w workspaces;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 SELECT * INTO ds FROM document_sets WHERE id=identity FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Запись недоступна'; END IF;
 PERFORM require_open_period(ds.occurred_on);
 SELECT * INTO w FROM workspaces WHERE id=ds.workspace_id;
 IF note IS NOT NULL AND length(note)>500 THEN RAISE EXCEPTION 'Причина: до 500 символов'; END IF;
 IF action_name='request_correction' THEN
  IF ds.author_user_id<>actor_user_id() OR coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Укажите причину исправления своей записи'; END IF;
  UPDATE document_sets SET correction_requested=true,request_text=note WHERE id=identity;
 ELSE
  IF w.owner_user_id<>actor_user_id() THEN RAISE EXCEPTION 'Проверка доступна руководителю'; END IF;
  IF action_name='accept' THEN
   IF w.documents_required AND ds.operation_kind='expense' AND ds.amount>=w.document_min_amount AND (w.document_category_id IS NULL OR w.document_category_id=ds.category_id) AND NOT EXISTS(SELECT 1 FROM documents WHERE set_id=identity AND state='active' AND (expires_at IS NULL OR expires_at>now())) AND coalesce(length(trim(note)),0)=0 THEN RAISE EXCEPTION 'Нужен документ либо явное исключение с причиной'; END IF;
   UPDATE document_sets SET status='accepted',financial_accepted=true,exception_reason=note,correction_requested=false,correction_approved=false WHERE id=identity;
  ELSIF action_name IN ('clarify','request_document','approve_correction') THEN
   IF coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Укажите причину'; END IF;
   IF action_name='approve_correction' AND NOT ds.correction_requested THEN RAISE EXCEPTION 'Нет запроса на исправление'; END IF;
   UPDATE document_sets SET status='clarification',request_text=note,correction_approved=action_name='approve_correction' WHERE id=identity;
  ELSE RAISE EXCEPTION 'Неизвестное действие'; END IF;
 END IF;
 INSERT INTO review_audit(workspace_id,set_id,actor_user_id,action,reason) VALUES(ds.workspace_id,identity,actor_user_id(),action_name,note);
END $$;
CREATE FUNCTION guard_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE ds document_sets; prior date;
BEGIN
 PERFORM require_open_period(NEW.occurred_on);
 SELECT occurred_on INTO prior FROM operation_revisions WHERE operation_id=NEW.operation_id ORDER BY revision_no DESC LIMIT 1;
 IF FOUND THEN PERFORM require_open_period(prior); END IF;
 SELECT * INTO ds FROM document_sets WHERE id=NEW.operation_id FOR UPDATE;
 IF FOUND THEN
  IF ds.financial_accepted AND NOT ds.correction_approved THEN RAISE EXCEPTION 'Запись принята. Сначала запросите исправление у руководителя: /correction ID | причина'; END IF;
  UPDATE document_sets SET status='unreviewed',financial_accepted=false,correction_requested=false,correction_approved=false,occurred_on=NEW.occurred_on,amount=NEW.amount,category_id=NEW.category_id WHERE id=ds.id;
  INSERT INTO review_audit(workspace_id,set_id,actor_user_id,action) VALUES(ds.workspace_id,ds.id,actor_user_id(),'revision_changed');
 END IF;RETURN NEW;
END $$;
CREATE TRIGGER revision_review_guard BEFORE INSERT ON operation_revisions FOR EACH ROW EXECUTE FUNCTION guard_revision();
CREATE FUNCTION guard_fund_date() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN PERFORM require_open_period(NEW.effective_on);RETURN NEW; END $$;
CREATE TRIGGER fund_period_guard BEFORE INSERT ON fund_entries FOR EACH ROW EXECUTE FUNCTION guard_fund_date();
CREATE FUNCTION set_closed_period(start_date date,end_date date,close_it boolean,note text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 IF NOT owns_workspace(current_workspace()) OR coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 OR end_date<start_date THEN RAISE EXCEPTION 'Закрытие и открытие: руководитель, корректный период и причина обязательны'; END IF;
 IF close_it THEN INSERT INTO closed_periods(workspace_id,start_on,end_on,reason) VALUES(current_workspace(),start_date,end_date,note);
 ELSE UPDATE closed_periods SET state='open' WHERE workspace_id=current_workspace() AND start_on=start_date AND end_on=end_date AND state='closed';IF NOT FOUND THEN RAISE EXCEPTION 'Закрытый период с такими границами не найден'; END IF; END IF;
 INSERT INTO review_audit(workspace_id,actor_user_id,action,reason) VALUES(current_workspace(),actor_user_id(),CASE WHEN close_it THEN 'period_closed' ELSE 'period_opened' END,start_date::text||' — '||end_date::text||': '||note);
END $$;
CREATE FUNCTION delete_document(identity uuid,confirm_it boolean,note text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE doc documents; ds document_sets;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 SELECT * INTO doc FROM documents WHERE id=identity FOR UPDATE;
 IF NOT FOUND OR doc.state='deleted' THEN RAISE EXCEPTION 'Документ недоступен'; END IF;
 SELECT * INTO ds FROM document_sets WHERE id=doc.set_id;
 PERFORM require_open_period(ds.occurred_on);
 IF coalesce(length(trim(note)),0) NOT BETWEEN 1 AND 500 THEN RAISE EXCEPTION 'Укажите причину удаления'; END IF;
 IF confirm_it AND NOT owns_workspace(doc.workspace_id) THEN RAISE EXCEPTION 'Удаление подтверждает руководитель'; END IF;
 UPDATE documents SET state=CASE WHEN confirm_it THEN 'deleted' ELSE 'delete_requested' END,deletion_reason=note WHERE id=identity;
 IF confirm_it AND doc.source_receipt_id IS NOT NULL THEN UPDATE receipt_files SET expires_at=now() WHERE id=doc.source_receipt_id; END IF;
 IF confirm_it THEN UPDATE document_quota SET bytes=bytes-doc.size_bytes WHERE workspace_id=doc.workspace_id; END IF;
 UPDATE document_sets SET status='unreviewed' WHERE id=doc.set_id;
 INSERT INTO review_audit(workspace_id,set_id,document_id,actor_user_id,action,reason) VALUES(doc.workspace_id,doc.set_id,doc.id,actor_user_id(),CASE WHEN confirm_it THEN 'document_deleted' ELSE 'document_delete_requested' END,note);
END $$;
CREATE FUNCTION document_policy(required boolean,minimum numeric,category uuid DEFAULT NULL) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 IF NOT owns_workspace(current_workspace()) OR minimum<0 OR (category IS NOT NULL AND NOT EXISTS(SELECT 1 FROM categories WHERE id=category)) THEN RAISE EXCEPTION 'Настройка документов доступна руководителю'; END IF;
 UPDATE workspaces SET documents_required=required,document_min_amount=minimum,document_category_id=category WHERE id=current_workspace();
 INSERT INTO review_audit(workspace_id,actor_user_id,action,reason) VALUES(current_workspace(),actor_user_id(),'document_policy',required::text||' / '||minimum::text||' / '||coalesce(category::text,'all'));
END $$;
REVOKE ALL ON FUNCTION require_open_period(date),ensure_document_set(text,uuid),add_document(uuid,uuid,text,integer,text,uuid,uuid),review_record(uuid,text,text),guard_revision(),guard_fund_date(),set_closed_period(date,date,boolean,text),delete_document(uuid,boolean,text),document_policy(boolean,numeric,uuid) FROM PUBLIC;

DROP POLICY own ON receipt_files;
CREATE POLICY own ON receipt_files USING(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id))) WITH CHECK(workspace_id=current_workspace() AND (author_user_id=actor_user_id() OR owns_workspace(workspace_id)));
CREATE FUNCTION document_quota_policy(file_count integer,megabytes integer) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(current_workspace()::text,11001));
 IF NOT owns_workspace(current_workspace()) OR file_count NOT BETWEEN 1 AND 100 OR megabytes NOT BETWEEN 15 AND 10240 THEN RAISE EXCEPTION 'Руководитель задаёт 1–100 файлов на запись и 15–10240 МБ на бюджет'; END IF;
 UPDATE workspaces SET document_file_limit=file_count,document_byte_limit=megabytes::bigint*1048576 WHERE id=current_workspace();
 INSERT INTO review_audit(workspace_id,actor_user_id,action,reason) VALUES(current_workspace(),actor_user_id(),'document_quota',file_count::text||' files / '||megabytes::text||' MB');
END $$;
CREATE FUNCTION guard_claim_date() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$ BEGIN PERFORM require_open_period(NEW.occurred_on);RETURN NEW; END $$;
CREATE TRIGGER claim_period_guard BEFORE INSERT ON fund_claims FOR EACH ROW EXECUTE FUNCTION guard_claim_date();
CREATE TRIGGER transfer_period_guard BEFORE INSERT ON fund_transfers FOR EACH ROW EXECUTE FUNCTION guard_claim_date();
REVOKE ALL ON FUNCTION document_quota_policy(integer,integer),guard_claim_date() FROM PUBLIC;
-- Authorized maintenance can pin a workspace without changing the user's menu selection.
CREATE OR REPLACE FUNCTION current_workspace() RETURNS uuid LANGUAGE sql STABLE SET search_path=balans,pg_temp AS $$
 SELECT coalesce((SELECT id FROM workspaces WHERE id::text=nullif(current_setting('balans.workspace_id',true),'')),(SELECT w.id FROM user_settings s JOIN workspaces w ON w.id=s.selected_workspace_id WHERE s.user_id=actor_user_id()),(SELECT id FROM workspaces WHERE owner_user_id=actor_user_id() AND kind='personal'))
$$;
-- Backfill review metadata under the schema owner; the financial rows stay unchanged.
ALTER TABLE document_sets NO FORCE ROW LEVEL SECURITY;
ALTER TABLE operations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE operation_revisions NO FORCE ROW LEVEL SECURITY;
ALTER TABLE fund_transfers NO FORCE ROW LEVEL SECURITY;
ALTER TABLE fund_claims NO FORCE ROW LEVEL SECURITY;
INSERT INTO document_sets(id,workspace_id,entity_kind,author_user_id,viewers,occurred_on,amount,category_id,operation_kind) SELECT o.id,o.workspace_id,'operation',o.created_by_user_id,ARRAY[o.created_by_user_id],r.occurred_on,r.amount,r.category_id,o.kind FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id ON CONFLICT DO NOTHING;
INSERT INTO document_sets(id,workspace_id,entity_kind,author_user_id,viewers,occurred_on,amount,operation_kind) SELECT id,workspace_id,'transfer',sender_user_id,ARRAY[sender_user_id,recipient_user_id],occurred_on,amount,'transfer' FROM fund_transfers ON CONFLICT DO NOTHING;
INSERT INTO document_sets(id,workspace_id,entity_kind,author_user_id,viewers,occurred_on,amount,operation_kind) SELECT id,workspace_id,'claim',author_user_id,ARRAY[author_user_id],occurred_on,amount,'income' FROM fund_claims ON CONFLICT DO NOTHING;
ALTER TABLE document_sets FORCE ROW LEVEL SECURITY;
ALTER TABLE operations FORCE ROW LEVEL SECURITY;
ALTER TABLE operation_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE fund_transfers FORCE ROW LEVEL SECURITY;
ALTER TABLE fund_claims FORCE ROW LEVEL SECURITY;
