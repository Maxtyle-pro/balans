"""Encrypted PostgreSQL + media backup. No DSN or subprocess error text is logged."""
import argparse,json,os,secrets,stat,subprocess,tarfile,tempfile,time
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID
import psycopg
from psycopg.conninfo import conninfo_to_dict
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from dotenv import load_dotenv
from balans.storage_lock import storage_lock

MAGIC=b'BALANS-BACKUP-1\n';CHUNK=1024*1024

def private_key(path,create=False):
    path=Path(path)
    if create:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32));f.flush();os.fsync(f.fileno())
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('Ключ должен быть обычным файлом с правами 600.')
        key=f.read(33)
    if len(key)!=32:raise ValueError('Нужен ключ из 32 байт.')
    return key


def pg_env(dsn):
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    mapping={'host':'PGHOST','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD','dbname':'PGDATABASE','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','connect_timeout':'PGCONNECT_TIMEOUT','options':'PGOPTIONS','application_name':'PGAPPNAME','hostaddr':'PGHOSTADDR','channel_binding':'PGCHANNELBINDING','passfile':'PGPASSFILE','service':'PGSERVICE'}
    for k,v in conninfo_to_dict(dsn).items():
        if k not in mapping:raise ValueError('Неподдерживаемый параметр подключения резервного копирования.')
        env[mapping[k]]=v
    return env


def pg_run(program,args,dsn):
    result=subprocess.run([program,*args],env=pg_env(dsn),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=3600)
    if result.returncode:raise RuntimeError(f'{program} завершился с кодом {result.returncode}; проверьте роль, версию PostgreSQL и доступность БД.')


def encrypt(source,destination,key):
    nonce=secrets.token_bytes(12);header=MAGIC+nonce
    ctx=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor();ctx.authenticate_additional_data(header)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with open(source,'rb') as src,os.fdopen(fd,'wb') as dst:
            dst.write(header)
            while block:=src.read(CHUNK):dst.write(ctx.update(block))
            dst.write(ctx.finalize());dst.write(ctx.tag);dst.flush();os.fsync(dst.fileno())
    except BaseException:Path(destination).unlink(missing_ok=True);raise


def decrypt(source,destination,key):
    with open(source,'rb') as src:
        header=src.read(len(MAGIC)+12)
        if not header.startswith(MAGIC):raise ValueError('Неизвестный формат копии.')
        src.seek(-16,2);end=src.tell();tag=src.read(16);src.seek(len(header))
        ctx=Cipher(algorithms.AES(key),modes.GCM(header[-12:],tag)).decryptor();ctx.authenticate_additional_data(header)
        try:
            with open(destination,'xb') as dst:
                os.chmod(destination,0o600)
                while src.tell()<end:dst.write(ctx.update(src.read(min(CHUNK,end-src.tell()))))
                dst.write(ctx.finalize())
        except BaseException:Path(destination).unlink(missing_ok=True);raise


def create_backup(dsn,storage,output,key):
    storage=Path(storage).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True,mode=0o700)
    for old in output.glob('balans-*.bkp'):
        if not old.is_symlink() and old.stat().st_mtime<time.time()-30*86400:old.unlink()
    with psycopg.connect(dsn) as c:
        if not c.execute('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()[0]:raise ValueError('Резервной роли нужен BYPASSRLS и SELECT, приложению эти права запрещены.')
    roots=[storage/'receipts',storage/'documents/personal',storage/'documents/shared']
    if not (storage/'privacy.key').is_file():raise ValueError('Сначала инициализируйте ключ приватности сервисом.')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(4)
    target=output/f'balans-{stamp}.bkp'
    with tempfile.TemporaryDirectory(prefix='backup-',dir=output) as tmp,ExitStack() as stack:
        tmp=Path(tmp)
        for root in roots:
            root.mkdir(parents=True,exist_ok=True,mode=0o700);stack.enter_context(storage_lock(root,exclusive=True))
        pg_run('pg_dump',['--format=custom','--no-owner','--no-privileges','--file',str(tmp/'database.dump')],dsn)
        (tmp/'manifest.json').write_text(json.dumps({'format':1,'created_at':datetime.now(timezone.utc).isoformat()}))
        with tarfile.open(tmp/'backup.tar','w') as archive:
            archive.add(tmp/'database.dump',arcname='database.dump');archive.add(tmp/'manifest.json',arcname='manifest.json')
            archive.add(storage/'privacy.key',arcname='storage/privacy.key')
            for root in roots:
                for path in root.glob('*.bin'):
                    if path.is_symlink():raise ValueError('Ссылки в хранилище запрещены.')
                    UUID(path.stem)
                    archive.add(path,arcname='storage/'+str(path.relative_to(storage)))
        encrypt(tmp/'backup.tar',target,key)
    # Privacy retention is enforced even when the next backup fails.
    for path in output.glob('balans-*.bkp'):
        if path!=target and not path.is_symlink() and path.stat().st_mtime<time.time()-30*86400:path.unlink()
    status=output/'last-success.json'
    status.write_text(json.dumps({'completed_at':datetime.now(timezone.utc).isoformat(),'file':target.name,'bytes':target.stat().st_size}));status.chmod(0o600)
    return target


def main():
    p=argparse.ArgumentParser();p.add_argument('--init-key',action='store_true');p.add_argument('--env-file',default='.env');args=p.parse_args();load_dotenv(args.env_file)
    keypath=os.getenv('BACKUP_KEY_FILE','')
    if not keypath:raise ValueError('Укажите BACKUP_KEY_FILE вне каталога резервных копий.')
    if args.init_key:private_key(keypath,True);print('Ключ создан с правами 600. Сохраните отдельную защищённую копию.');return
    dsn=os.getenv('BACKUP_DATABASE_URL','')
    if not dsn:raise ValueError('Настройте отдельный BACKUP_DATABASE_URL.')
    root=Path(os.getenv('RECEIPT_STORAGE_DIR','.local/receipts')).resolve().parent
    path=create_backup(dsn,root,os.getenv('BACKUP_DIR','.local/backups'),private_key(keypath));print(f'Зашифрованная копия создана: {path.name}')

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Резервное копирование не завершено: '+type(exc).__name__) from None
