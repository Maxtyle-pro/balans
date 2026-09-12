"""Upgrade a populated legacy schema, not only an empty test database."""
import hashlib
import os
from pathlib import Path
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from balans.migrate import migrate
from scripts import dev_setup


def test_upgrade_preserves_existing_trial_and_usage(monkeypatch):
    root=Path(__file__).resolve().parents[1]/'migrations'
    def legacy_migrate(dsn,runtime):
        with psycopg.connect(dsn) as c:
            c.execute('CREATE TABLE public.schema_migrations(name text PRIMARY KEY,checksum text NOT NULL)')
            for path in sorted(root.glob('*.sql')):
                if path.name>='020_onboarding.sql':break
                source=path.read_text();c.execute(source)
                c.execute('INSERT INTO public.schema_migrations VALUES(%s,%s)',(path.name,hashlib.sha256(source.encode()).hexdigest()))
    monkeypatch.setattr(dev_setup,'migrate',legacy_migrate)
    admin=os.getenv('POSTGRES_ADMIN_DSN','dbname=postgres host=localhost')
    name='balans_test_upgrade_'+secrets.token_hex(5)
    owner_dsn,_,owner,runtime=dev_setup.provision(admin,name)
    actor=220000001
    try:
        with psycopg.connect(owner_dsn) as c:
            c.execute('SET search_path=balans,pg_catalog')
            c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(actor),))
            user=c.execute('SELECT bootstrap()').fetchone()[0]
            c.execute("UPDATE billing_config SET enabled=true,stars=100,trial_days=7,text_quota=1000,voice_seconds=3600,image_quota=100")
            before=c.execute('SELECT billing_access()').fetchone()[0]
            c.execute("SET LOCAL balans.billing_worker='on'")
            c.execute("UPDATE billing_accounts SET created_at=now()-interval '2 days'")
            c.execute("UPDATE billing_config SET enabled_at=now()-interval '3 days'")
            before=c.execute('SELECT billing_access()').fetchone()[0]
            c.execute("INSERT INTO quota_reservations(job_id,kind,workspace_id,sponsor_id,author_user_id,units,period_start,state) VALUES(%s,'text',current_workspace(),%s,%s,3,date_trunc('month',now()),'consumed')",(uuid4(),user,user))
        migrate(owner_dsn,runtime)
        with psycopg.connect(owner_dsn) as c:
            c.execute('SET search_path=balans,pg_catalog')
            c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(actor),))
            after=c.execute('SELECT billing_access()').fetchone()[0]
            assert before['until']==after['until'] and after['status']=='trial'
            assert after['quotas']['text']==1000
            assert c.execute('SELECT billing_usage()').fetchone()[0]=={'text':3}
            c.execute('UPDATE billing_config SET trial_days=90,text_quota=5000')
            assert c.execute('SELECT billing_access()').fetchone()[0]['until']==before['until']
        migrate(owner_dsn,runtime)
    finally:
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in (runtime,owner):c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
