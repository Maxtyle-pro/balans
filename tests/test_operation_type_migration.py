"""Run the UX migration against populated pre-034 data without an actor."""
import hashlib
import os
from pathlib import Path
import secrets
import psycopg
from psycopg import sql
from balans.migrate import migrate
from scripts import dev_setup


def test_upgrade_preserves_operations_postings_and_rls(monkeypatch):
    root=Path(__file__).resolve().parents[1]/'migrations'
    def legacy_migrate(dsn,runtime):
        with psycopg.connect(dsn) as c:
            c.execute('CREATE TABLE public.schema_migrations(name text PRIMARY KEY,checksum text NOT NULL)')
            for path in sorted(root.glob('*.sql')):
                if path.name>='034_operation_type.sql':break
                source=path.read_text();c.execute(source)
                c.execute('INSERT INTO public.schema_migrations VALUES(%s,%s)',(path.name,hashlib.sha256(source.encode()).hexdigest()))
    monkeypatch.setattr(dev_setup,'migrate',legacy_migrate)
    admin=os.getenv('POSTGRES_ADMIN_DSN','dbname=postgres host=localhost')
    name='balans_test_type_upgrade_'+secrets.token_hex(5)
    owner_dsn,runtime_dsn,owner,runtime=dev_setup.provision(admin,name)
    try:
        with psycopg.connect(owner_dsn) as c:
            c.execute('SET search_path=balans,pg_catalog')
            c.execute("SET LOCAL balans.telegram_user_id='399000001'")
            c.execute('SELECT bootstrap()')
            c.execute("SELECT choose_user_currency('RUB')")
            draft=c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,category_id,amount,description,occurred_on,timezone_snapshot,source_sent_at,step,kind) SELECT current_workspace(),actor_user_id(),a.id,uncategorized_category(current_workspace()),150,'До обновления',current_date,'Europe/Moscow',now(),'confirm','expense' FROM accounts a WHERE a.workspace_id=current_workspace() LIMIT 1 RETURNING id").fetchone()[0]
            identity=c.execute('SELECT save_expense(%s)',(draft,)).fetchone()[0]
            balance=c.execute('SELECT sum(delta) FROM postings').fetchone()[0]
        migrate(owner_dsn,runtime)
        migrate(owner_dsn,runtime)
        with psycopg.connect(runtime_dsn) as c:
            c.execute('SET search_path=balans,pg_catalog')
            c.execute("SET LOCAL balans.telegram_user_id='399000001'")
            assert c.execute('SELECT operation_kind FROM operation_revisions WHERE operation_id=%s',(identity,)).fetchone()==('expense',)
            assert c.execute('SELECT sum(delta) FROM postings').fetchone()[0]==balance
            assert c.execute("SELECT bool_and(relforcerowsecurity) FROM pg_class WHERE oid IN ('operations'::regclass,'operation_revisions'::regclass)").fetchone()==(True,)
            assert not c.execute("SELECT has_table_privilege(current_user,'postings','INSERT,UPDATE,DELETE')").fetchone()[0]
            c.execute("SET LOCAL balans.telegram_user_id='399000002'")
            c.execute('SELECT bootstrap()')
            assert c.execute('SELECT count(*) FROM operations').fetchone()==(0,)
    finally:
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in (runtime,owner):c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
