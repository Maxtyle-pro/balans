"""Restore ONLY into an empty offline database and an empty storage directory."""
import argparse,hashlib,hmac,json,os,shutil,tarfile,tempfile
from pathlib import Path
from uuid import UUID
import psycopg
from dotenv import load_dotenv
from scripts.backup import decrypt,private_key,pg_run
from balans.migrate import migrate
from balans.service import Service
from balans.receipt_media import ReceiptStorage


def verify_registry(path,key):
    result=[]
    for line in Path(path).read_text().splitlines():
        record=json.loads(line);data=record['data']
        raw=json.dumps(data,sort_keys=True,separators=(',',':')).encode()
        if not hmac.compare_digest(record['signature'],hmac.new(key,raw,hashlib.sha256).hexdigest()):raise ValueError('Подпись реестра удалений неверна.')
        UUID(data['id']);UUID(data['user_id'])
        if len(data['telegram_hash'])!=64:raise ValueError('Некорректный реестр.')
        result.append(data)
    return result


def replay_deletions(service,owner_dsn,records):
    applied=0
    for data in records:
        with psycopg.connect(owner_dsn) as c:
            c.execute("SET LOCAL balans.erasure_worker='on'")
            existing=c.execute('SELECT state FROM balans.erasure_requests WHERE telegram_hash=%s',(data['telegram_hash'],)).fetchone()
            if existing and existing[0]!='preview':continue
            c.execute('ALTER TABLE balans.users NO FORCE ROW LEVEL SECURITY')
            user=c.execute('SELECT telegram_user_id FROM balans.users WHERE id=%s',(data['user_id'],)).fetchone()
            c.execute('ALTER TABLE balans.users FORCE ROW LEVEL SECURITY')
        if not user:continue
        actor=user[0]
        if not hmac.compare_digest(service._actor_hash(actor),data['telegram_hash']):raise ValueError('Реестр не соответствует ключу/пользователю копии.')
        with service._actor_transaction(actor) as c:
            row=c.execute("INSERT INTO erasure_requests(id,user_id,telegram_hash,telegram_user_id) VALUES(%s,%s,%s,%s) ON CONFLICT(telegram_hash) DO UPDATE SET expires_at=now()+interval '1 hour' RETURNING id",(data['id'],data['user_id'],data['telegram_hash'],actor)).fetchone()
            c.execute('SELECT confirm_erasure(%s)',(row['id'],))
        applied+=1
    # Drain all old queued erasures too, before any restored data can be served.
    while True:
        with psycopg.connect(owner_dsn) as c:
            c.execute("SET LOCAL balans.erasure_worker='on'")
            remaining=c.execute("SELECT count(*) FROM balans.erasure_requests WHERE state IN ('queued','files_pending')").fetchone()[0]
        if not remaining:break
        service.process_erasures()
    return applied


def restore_backup(archive,key,registry,owner_dsn,runtime_dsn,runtime_role,storage):
    storage=Path(storage).resolve()
    if storage.exists() and any(storage.iterdir()):raise ValueError('Каталог восстановления должен быть пустым.')
    with psycopg.connect(owner_dsn) as c:
        if c.execute("SELECT 1 FROM pg_class WHERE relnamespace IN (SELECT oid FROM pg_namespace WHERE nspname IN ('balans','public')) AND relkind IN ('r','p','v','S') LIMIT 1").fetchone():raise ValueError('База восстановления должна быть пустой.')
    storage.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Authenticated decryption and registry validation happen before database changes.
    with tempfile.TemporaryDirectory(prefix='restore-',dir=storage.parent) as tmp:
        tmp=Path(tmp);decrypt(archive,tmp/'backup.tar',key)
        with tarfile.open(tmp/'backup.tar') as tar:
            seen=set()
            for member in tar:
                name=member.name
                allowed=name in ('database.dump','manifest.json','storage/privacy.key')
                if name.startswith(('storage/receipts/','storage/documents/personal/','storage/documents/shared/')):
                    try:allowed=Path(name).suffix=='.bin' and str(UUID(Path(name).stem))+'.bin'==Path(name).name and str(Path(name).parent) in ('storage/receipts','storage/documents/personal','storage/documents/shared')
                    except ValueError:allowed=False
                if not allowed or not member.isfile() or name in seen or '..' in Path(name).parts:raise ValueError('Небезопасный состав копии.')
                seen.add(name);tar.extract(member,tmp,filter='data')
                (tmp/name).chmod(0o600)
        if not {'database.dump','manifest.json','storage/privacy.key'}<=seen:raise ValueError('Копия неполная.')
        records=verify_registry(registry,private_key(tmp/'storage/privacy.key'))
        pg_run('pg_restore',['--no-owner','--no-privileges','--exit-on-error','--single-transaction','--dbname','',str(tmp/'database.dump')],owner_dsn)
        migrate(owner_dsn,runtime_role)
        shutil.copytree(tmp/'storage',storage,dirs_exist_ok=True)
        shutil.copyfile(registry,storage/'deletions.jsonl');(storage/'deletions.jsonl').chmod(0o600)
    service=Service(runtime_dsn,receipt_storage=ReceiptStorage(storage/'receipts'))
    try:
        service.check();applied=replay_deletions(service,owner_dsn,records);service.purge_private_files()
        # Restoring historical outbound work must not charge, expose sessions or send old notices.
        with psycopg.connect(owner_dsn) as c:
            tables=('telegram_inbox','notification_outbox','admin_sessions','admin_login_codes','diagnostic_grants')
            for table in tables:c.execute(f'ALTER TABLE balans.{table} NO FORCE ROW LEVEL SECURITY')
            c.execute("UPDATE balans.telegram_inbox SET state='failed',lease_token=NULL,lease_until=NULL,error_code='restore_review_required' WHERE state IN ('pending','processing')")
            c.execute("UPDATE balans.notification_outbox SET state='uncertain' WHERE state IN ('pending','sending')")
            c.execute('DELETE FROM balans.admin_sessions');c.execute('DELETE FROM balans.admin_login_codes')
            c.execute("UPDATE balans.diagnostic_grants SET revoked=true,payload='{}'")
            c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            for table in tables:c.execute(f'ALTER TABLE balans.{table} FORCE ROW LEVEL SECURITY')
        # Files added after the DB snapshot are harmless extras, but cannot remain after erasure.
        with psycopg.connect(owner_dsn) as c:
            for table in ('receipt_files','documents'):c.execute(f'ALTER TABLE balans.{table} NO FORCE ROW LEVEL SECURITY')
            receipts={str(r[0]) for r in c.execute('SELECT id FROM balans.receipt_files')}
            docs={str(r[0]) for r in c.execute("SELECT id FROM balans.documents WHERE state<>'deleted'")}
            for table in ('receipt_files','documents'):c.execute(f'ALTER TABLE balans.{table} FORCE ROW LEVEL SECURITY')
        for root,valid in ((service.receipt_storage.root,receipts),(service.document_storage.personal.root,docs),(service.document_storage.shared.root,docs)):
            for path in root.glob('*.bin'):
                if path.stem not in valid:path.unlink()
        return applied
    finally:service.close()


def main():
    load_dotenv();p=argparse.ArgumentParser();p.add_argument('archive');p.add_argument('--registry',required=True);p.add_argument('--storage',required=True);args=p.parse_args()
    from psycopg.conninfo import conninfo_to_dict
    owner=os.environ['RESTORE_OWNER_DSN'];runtime=os.environ['RESTORE_RUNTIME_DSN'];role=conninfo_to_dict(runtime)['user']
    count=restore_backup(args.archive,private_key(os.environ['BACKUP_KEY_FILE']),args.registry,owner,runtime,role,args.storage)
    print(f'Изолированная копия восстановлена; повторно применено удалений: {count}. Выполните проверки перед переключением бота.')

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Восстановление не завершено: '+type(exc).__name__) from None
