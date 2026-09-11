import os,secrets,json
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from cryptography.exceptions import InvalidTag
from scripts.backup import encrypt,decrypt,create_backup
from scripts.restore import restore_backup,verify_registry
from scripts.dev_setup import provision
from balans.service import Service
from balans.receipt_media import ReceiptStorage
from test_service import send,query
from test_documents import op,attach
from test_privacy import erase


def test_encryption_rejects_tampering(tmp_path):
    key=secrets.token_bytes(32);source=tmp_path/'source';source.write_bytes(b'private'*200000)
    encrypted=tmp_path/'copy';encrypt(source,encrypted,key)
    decrypted=tmp_path/'plain';decrypt(encrypted,decrypted,key);assert decrypted.read_bytes()==source.read_bytes()
    blob=bytearray(encrypted.read_bytes());blob[len(blob)//2]^=1;encrypted.write_bytes(blob);decrypted.unlink()
    with pytest.raises(InvalidTag):decrypt(encrypted,decrypted,key)
    assert not decrypted.exists()


def test_restore_replays_latest_deletion_and_preserves_document(tmp_path):
    admin=os.getenv('POSTGRES_ADMIN_DSN','dbname=postgres host=localhost')
    name='balans_restore_'+secrets.token_hex(5);target=name+'_target'
    owner_dsn,runtime_dsn,owner,runtime=provision(admin,name)
    s=Service(runtime_dsn,receipt_storage=ReceiptStorage(tmp_path/'source/receipts'))
    restored=None
    try:
        db=(owner_dsn,runtime_dsn,runtime);deleted,kept=200000001,200000002
        operation=op(s,deleted,db);attach(s,deleted,operation)
        deleted_file=query(db,deleted,'SELECT id FROM documents')[0][0]
        operation=op(s,kept,db);attach(s,kept,operation)
        kept_file=query(db,kept,'SELECT id FROM documents')[0][0];original=s.document_storage.personal.path(kept_file).read_bytes()
        key=secrets.token_bytes(32)
        archive=create_backup(make_conninfo(admin,dbname=name),tmp_path/'source',tmp_path/'backups',key)
        erase(s,deleted);s.process_erasures()
        registry=tmp_path/'source/deletions.jsonl'
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(target),sql.Identifier(owner)))
            c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(target),sql.Identifier(runtime)))
        restored_owner=make_conninfo(owner_dsn,dbname=target);restored_runtime=make_conninfo(runtime_dsn,dbname=target)
        assert restore_backup(archive,key,registry,restored_owner,restored_runtime,runtime,tmp_path/'restored')==1
        restored=Service(restored_runtime,receipt_storage=ReceiptStorage(tmp_path/'restored/receipts'))
        assert 'Личные данные удалены' in send(restored,deleted,'/history').text
        assert '100,00' in send(restored,kept,'/history').text
        assert not restored.document_storage.personal.path(deleted_file).exists()
        assert restored.document_storage.personal.path(kept_file).read_bytes()==original
        with pytest.raises(ValueError):restore_backup(archive,key,registry,restored_owner,restored_runtime,runtime,tmp_path/'restored')
        data=json.loads(registry.read_text().splitlines()[0]);data['data']['telegram_hash']='0'*64
        bad=tmp_path/'bad.jsonl';bad.write_text(json.dumps(data)+'\n')
        with pytest.raises(ValueError):verify_registry(bad,s._privacy_key())
    finally:
        s.close()
        if restored:restored.close()
        with psycopg.connect(admin,autocommit=True) as c:
            for database in (target,name):c.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
            for role in (runtime,owner):c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
