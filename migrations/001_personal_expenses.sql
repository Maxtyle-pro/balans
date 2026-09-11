CREATE SCHEMA balans;
REVOKE ALL ON SCHEMA balans FROM PUBLIC;
SET search_path = balans, pg_catalog;
CREATE FUNCTION actor_telegram_id() RETURNS bigint LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('balans.telegram_user_id', true), '')::bigint
$$;
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_user_id bigint NOT NULL UNIQUE CHECK (telegram_user_id > 0),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION actor_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT id FROM balans.users WHERE telegram_user_id=balans.actor_telegram_id() AND status='active'
$$;
CREATE TABLE currencies (code text PRIMARY KEY, minor_units smallint NOT NULL CHECK (minor_units BETWEEN 0 AND 6));
INSERT INTO currencies VALUES ('RUB',2);
CREATE TABLE user_settings (
  user_id uuid PRIMARY KEY REFERENCES users,
  timezone text NOT NULL DEFAULT 'Europe/Moscow',
  default_currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies,
  save_mode text NOT NULL DEFAULT 'confirm' CHECK (save_mode='confirm')
);
CREATE TABLE workspaces (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES users,
  kind text NOT NULL DEFAULT 'personal' CHECK (kind='personal'),
  name text NOT NULL DEFAULT 'Личный бюджет',
  base_currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_user_id,kind)
);
CREATE TABLE memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces,
  user_id uuid NOT NULL REFERENCES users,
  role text NOT NULL DEFAULT 'owner' CHECK (role='owner'),
  status text NOT NULL DEFAULT 'active' CHECK (status='active'),
  UNIQUE(workspace_id,user_id), UNIQUE(workspace_id,id)
);
CREATE TABLE accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces,
  responsible_membership_id uuid NOT NULL,
  name text NOT NULL DEFAULT 'Основной',
  kind text NOT NULL DEFAULT 'custom' CHECK(kind='custom'),
  currency text NOT NULL DEFAULT 'RUB' REFERENCES currencies,
  UNIQUE(workspace_id,id), UNIQUE(workspace_id,id,currency),
  FOREIGN KEY(workspace_id,responsible_membership_id) REFERENCES memberships(workspace_id,id)
);
CREATE TABLE categories (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces,
  name text NOT NULL,
  kind text NOT NULL DEFAULT 'expense' CHECK(kind='expense'),
  UNIQUE(workspace_id,id), UNIQUE(workspace_id,name)
);
CREATE TABLE operation_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces,
  author_user_id uuid NOT NULL REFERENCES users,
  account_id uuid NOT NULL,
  category_id uuid,
  amount numeric(20,6) CHECK(amount > 0 AND amount <= 999999999999.99 AND amount=round(amount,2)),
  description text CHECK(length(description)<=500),
  occurred_on date,
  timezone_snapshot text NOT NULL,
  source_sent_at timestamptz NOT NULL,
  step text NOT NULL DEFAULT 'amount' CHECK(step IN ('amount','category','description','date','confirm')),
  state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','saved','cancelled')),
  expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id),
  FOREIGN KEY(workspace_id,category_id) REFERENCES categories(workspace_id,id)
);
CREATE UNIQUE INDEX one_pending_draft ON operation_drafts(author_user_id) WHERE state='pending';
CREATE TABLE operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  created_by_user_id uuid NOT NULL REFERENCES users, responsible_membership_id uuid NOT NULL,
  kind text NOT NULL DEFAULT 'expense' CHECK(kind='expense'),
  state text NOT NULL DEFAULT 'active' CHECK(state='active'),
  current_revision_id uuid NOT NULL, source_draft_id uuid NOT NULL UNIQUE,
  idempotency_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,id), UNIQUE(workspace_id,idempotency_key),
  FOREIGN KEY(workspace_id,source_draft_id) REFERENCES operation_drafts(workspace_id,id),
  FOREIGN KEY(workspace_id,responsible_membership_id) REFERENCES memberships(workspace_id,id)
);
CREATE TABLE operation_revisions (
  id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES workspaces, operation_id uuid NOT NULL,
  revision_no integer NOT NULL DEFAULT 1 CHECK(revision_no>0),
  account_id uuid NOT NULL, category_id uuid NOT NULL,
  amount numeric(20,6) NOT NULL CHECK(amount>0 AND amount<=999999999999.99 AND amount=round(amount,2)),
  currency text NOT NULL REFERENCES currencies, description text NOT NULL CHECK(length(description)<=500),
  occurred_on date NOT NULL, timezone_snapshot text NOT NULL,
  source_kind text NOT NULL DEFAULT 'manual' CHECK(source_kind='manual'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,id), UNIQUE(workspace_id,operation_id,id), UNIQUE(operation_id,revision_no),
  FOREIGN KEY(workspace_id,operation_id) REFERENCES operations(workspace_id,id),
  FOREIGN KEY(workspace_id,account_id,currency) REFERENCES accounts(workspace_id,id,currency),
  FOREIGN KEY(workspace_id,category_id) REFERENCES categories(workspace_id,id)
);
ALTER TABLE operations ADD FOREIGN KEY(workspace_id,id,current_revision_id)
  REFERENCES operation_revisions(workspace_id,operation_id,id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE journal_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  revision_id uuid NOT NULL UNIQUE, event_kind text NOT NULL DEFAULT 'expense' CHECK(event_kind='expense'),
  effective_on date NOT NULL, idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,id), UNIQUE(workspace_id,idempotency_key),
  FOREIGN KEY(workspace_id,revision_id) REFERENCES operation_revisions(workspace_id,id)
);
CREATE TABLE postings (
  workspace_id uuid NOT NULL REFERENCES workspaces, entry_id uuid NOT NULL, line_no integer NOT NULL CHECK(line_no=1),
  account_id uuid NOT NULL, currency text NOT NULL REFERENCES currencies,
  delta numeric(20,6) NOT NULL CHECK(delta<0 AND delta=round(delta,2)),
  PRIMARY KEY(entry_id,line_no),
  FOREIGN KEY(workspace_id,entry_id) REFERENCES journal_entries(workspace_id,id),
  FOREIGN KEY(workspace_id,account_id,currency) REFERENCES accounts(workspace_id,id,currency)
);
CREATE TABLE audit_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL REFERENCES workspaces,
  actor_user_id uuid NOT NULL REFERENCES users, operation_id uuid NOT NULL,
  action text NOT NULL CHECK(action='expense_created'), occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(workspace_id,operation_id) REFERENCES operations(workspace_id,id)
);
CREATE TABLE telegram_updates (
  bot_id bigint NOT NULL, update_id bigint NOT NULL, user_id uuid NOT NULL REFERENCES users,
  response jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(bot_id,update_id)
);
CREATE INDEX history_idx ON operations(workspace_id,created_at DESC,id DESC);
CREATE INDEX revision_date_idx ON operation_revisions(workspace_id,occurred_on);
CREATE INDEX postings_account_idx ON postings(workspace_id,account_id);
CREATE INDEX drafts_owner_idx ON operation_drafts(author_user_id);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY self ON users USING (telegram_user_id=actor_telegram_id()) WITH CHECK(telegram_user_id=actor_telegram_id());
ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY self ON user_settings USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON workspaces USING(owner_user_id=actor_user_id()) WITH CHECK(owner_user_id=actor_user_id());
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['memberships','accounts','categories','operation_drafts','operations','operation_revisions','journal_entries','postings','audit_log'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY own ON %I USING (workspace_id IN (SELECT id FROM balans.workspaces)) WITH CHECK (workspace_id IN (SELECT id FROM balans.workspaces))',t);
  END LOOP;
END $$;
ALTER TABLE telegram_updates ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_updates FORCE ROW LEVEL SECURITY;
CREATE POLICY self ON telegram_updates USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());

CREATE FUNCTION bootstrap() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = balans, pg_temp AS $$
DECLARE u uuid; w uuid; m uuid;
BEGIN
  INSERT INTO users(telegram_user_id) VALUES(actor_telegram_id()) ON CONFLICT DO NOTHING;
  u := actor_user_id();
  IF u IS NULL THEN RAISE EXCEPTION 'Access denied'; END IF;
  IF EXISTS(SELECT 1 FROM workspaces WHERE owner_user_id=u) THEN RETURN u; END IF;
  INSERT INTO user_settings(user_id) VALUES(u);
  INSERT INTO workspaces(owner_user_id) VALUES(u) RETURNING id INTO w;
  INSERT INTO memberships(workspace_id,user_id) VALUES(w,u) RETURNING id INTO m;
  INSERT INTO accounts(workspace_id,responsible_membership_id) VALUES(w,m);
  INSERT INTO categories(workspace_id,name) SELECT w,unnest(ARRAY['Продукты','Кафе и рестораны','Транспорт','Дом','Здоровье','Покупки','Развлечения','Другое']);
  RETURN u;
END $$;

CREATE FUNCTION save_expense(draft_id uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = balans, pg_temp AS $$
DECLARE d operation_drafts; o uuid; r uuid:=gen_random_uuid(); e uuid; m uuid; c text;
BEGIN
  -- RLS and explicit author check apply even to callbacks with a substituted UUID.
  SELECT * INTO d FROM operation_drafts WHERE id=draft_id AND author_user_id=actor_user_id() FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Draft unavailable'; END IF;
  SELECT id INTO o FROM operations WHERE source_draft_id=d.id;
  IF FOUND THEN RETURN o; END IF;
  IF d.state<>'pending' OR d.step<>'confirm' OR d.expires_at<=now()
    OR d.amount IS NULL OR d.category_id IS NULL OR d.description IS NULL OR d.occurred_on IS NULL
    THEN RAISE EXCEPTION 'Draft incomplete or expired'; END IF;
  SELECT responsible_membership_id,currency INTO m,c FROM accounts WHERE id=d.account_id AND workspace_id=d.workspace_id;
  INSERT INTO operations(workspace_id,created_by_user_id,responsible_membership_id,current_revision_id,source_draft_id,idempotency_key)
    VALUES(d.workspace_id,actor_user_id(),m,r,d.id,d.id::text) RETURNING id INTO o;
  INSERT INTO operation_revisions(id,workspace_id,operation_id,account_id,category_id,amount,currency,description,occurred_on,timezone_snapshot)
    VALUES(r,d.workspace_id,o,d.account_id,d.category_id,d.amount,c,d.description,d.occurred_on,d.timezone_snapshot);
  INSERT INTO journal_entries(workspace_id,revision_id,effective_on,idempotency_key)
    VALUES(d.workspace_id,r,d.occurred_on,d.id::text) RETURNING id INTO e;
  INSERT INTO postings(workspace_id,entry_id,line_no,account_id,currency,delta) VALUES(d.workspace_id,e,1,d.account_id,c,-d.amount);
  INSERT INTO audit_log(workspace_id,actor_user_id,operation_id,action) VALUES(d.workspace_id,actor_user_id(),o,'expense_created');
  UPDATE operation_drafts SET state='saved' WHERE id=d.id;
  RETURN o;
END $$;

-- Validate the complete operation at COMMIT, after all related rows exist.
CREATE FUNCTION check_expense() RETURNS trigger LANGUAGE plpgsql SET search_path=balans,pg_temp AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id
    JOIN journal_entries e ON e.revision_id=r.id JOIN postings p ON p.entry_id=e.id
    JOIN accounts a ON a.id=r.account_id
    WHERE o.id=NEW.id AND p.delta=-r.amount AND p.currency=r.currency AND p.account_id=r.account_id
      AND e.effective_on=r.occurred_on AND a.responsible_membership_id=o.responsible_membership_id
  ) THEN RAISE EXCEPTION 'Incomplete expense journal'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER expense_complete AFTER INSERT ON operations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_expense();
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA balans FROM PUBLIC;
