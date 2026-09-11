"""Create an isolated local PostgreSQL database; preserve any existing .env."""
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from balans.migrate import migrate


def provision(admin_dsn, name):
    owner, runtime = name + '_owner', name + '_app'
    owner_password, runtime_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        for role, password in ((owner, owner_password), (runtime, runtime_password)):
            c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        c.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name), sql.Identifier(owner)))
        c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
        c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(name), sql.Identifier(runtime)))
    owner_dsn = make_conninfo(admin_dsn, dbname=name, user=owner, password=owner_password)
    runtime_dsn = make_conninfo(admin_dsn, dbname=name, user=runtime, password=runtime_password)
    migrate(owner_dsn, runtime)
    return owner_dsn, runtime_dsn, owner, runtime


if __name__ == '__main__':
    env = Path(__file__).resolve().parents[1] / '.env'
    if env.exists():
        raise SystemExit('.env уже существует; настройки не перезаписаны.')
    name = 'balans_dev_' + secrets.token_hex(4)
    owner_dsn, runtime_dsn, _, _ = provision(os.getenv('POSTGRES_ADMIN_DSN', 'dbname=postgres host=localhost'), name)
    # Generated libpq connection strings contain no single quotes in generated passwords.
    content = f"BOT_TOKEN=\nDATABASE_URL='{runtime_dsn}'\nMIGRATION_DATABASE_URL='{owner_dsn}'\nSUPPORT_CONTACT=\n"
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    print(f'Создана отдельная локальная БД {name}, миграции применены. В .env осталось заполнить BOT_TOKEN.')
