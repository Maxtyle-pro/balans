"""Provision a separate read-only BYPASSRLS role; never grant it to the bot."""
import os,secrets
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo,conninfo_to_dict
from dotenv import load_dotenv
from scripts.backup import private_key
from balans.service import Service


def main():
    load_dotenv();destination=Path('.local/backup.env').resolve()
    if destination.exists():raise ValueError('Конфигурация резервирования уже существует; не перезаписана.')
    runtime=conninfo_to_dict(os.environ['DATABASE_URL']);owner=conninfo_to_dict(os.environ['MIGRATION_DATABASE_URL'])['user'];database=runtime['dbname']
    role=database+'_backup';password=secrets.token_urlsafe(32)
    if len(role.encode())>63:raise ValueError('Слишком длинное имя БД для резервной роли.')
    admin=make_conninfo(os.getenv('POSTGRES_ADMIN_DSN','dbname=postgres host=localhost'),dbname=database)
    with psycopg.connect(admin) as c:
        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database),sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA balans,public TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA balans,public TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA balans,public GRANT SELECT ON TABLES TO {}').format(sql.Identifier(owner),sql.Identifier(role)))
    root=Path(os.getenv('RECEIPT_STORAGE_DIR','.local/receipts')).resolve().parent;root.mkdir(parents=True,exist_ok=True,mode=0o700)
    key=root/'backup.key';private_key(key,True)
    s=Service(os.environ['DATABASE_URL'])
    try:
        s._privacy_key()
        registry=root/'deletions.jsonl'
        if not registry.exists():
            with s.pool.connection() as c,c.transaction():
                c.execute("SET LOCAL balans.erasure_worker='on'")
                if c.execute("SELECT count(*) AS n FROM balans.erasure_requests WHERE state<>'preview'").fetchone()['n']:raise ValueError('Для существующих удалений необходим оригинальный реестр.')
            with os.fdopen(os.open(registry,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.flush();os.fsync(f.fileno())
    finally:s.close()
    dsn=make_conninfo(os.environ['DATABASE_URL'],user=role,password=password)
    # Generated conninfo values here are produced locally; dotenv double-quote escaping is explicit.
    def quoted(value):return '"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'
    values={'BACKUP_DATABASE_URL':dsn,'BACKUP_KEY_FILE':key,'BACKUP_DIR':root/'backups','RECEIPT_STORAGE_DIR':root/'receipts'}
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write('\n'.join(k+'='+quoted(v) for k,v in values.items())+'\n')
    print('Созданы отдельная резервная роль, ключ и .local/backup.env; секреты не выводятся.')

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Настройка резервирования не завершена: '+type(exc).__name__) from None
