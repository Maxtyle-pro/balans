import os
import secrets

import psycopg
from psycopg import sql
import pytest

from scripts.dev_setup import provision
from balans.service import Service


@pytest.fixture(scope='session')
def database():
    admin = os.getenv('POSTGRES_ADMIN_DSN', 'dbname=postgres host=localhost')
    name = 'balans_test_' + secrets.token_hex(6)
    owner_dsn, runtime_dsn, owner, runtime = provision(admin, name)
    with psycopg.connect(owner_dsn) as setup:
        # Legacy scenarios exercise the optional confirmation mode. Capture tests
        # enable the shipping mode explicitly; verify its migration default first.
        default=setup.execute("SELECT column_default FROM information_schema.columns WHERE table_schema='balans' AND table_name='user_settings' AND column_name='automatic_capture'").fetchone()[0]
        assert default=='true'
        setup.execute('ALTER TABLE balans.user_settings ALTER COLUMN automatic_capture SET DEFAULT false')
    try:
        yield owner_dsn, runtime_dsn, runtime
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in (runtime, owner):
                c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.fixture(autouse=True)
def isolated_file_storage(tmp_path,monkeypatch):
    # Every Service in a test (including restarted instances) uses disposable files.
    monkeypatch.setenv('RECEIPT_STORAGE_DIR',str(tmp_path/'receipts'))


@pytest.fixture
def service(database):
    result = Service(database[1])
    result.check()
    yield result
    result.close()
