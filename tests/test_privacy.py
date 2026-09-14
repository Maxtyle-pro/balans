from itertools import count
from uuid import UUID
import json
import psycopg
import pytest
from balans.service import Service
from balans.receipt_media import ReceiptStorage
from test_service import send,query,draft
from test_receipts import button
from test_documents import op,attach
from test_workspaces import admit

USERS=count(170000000)

@pytest.fixture
def private_service(database,tmp_path):
    s=Service(database[1],receipt_storage=ReceiptStorage(tmp_path/'receipts'))
    yield s
    s.close()


def erase(s,u):
    preview=send(s,u,'/delete')
    return send(s,u,callback=button(preview,'Подтверждаю удаление личного профиля'))


def test_erasure_blocks_immediately_then_removes_data_and_files(private_service,database):
    s=private_service;u=next(USERS);identity=op(s,u,database);attach(s,u,identity)
    doc=query(database,u,'SELECT id FROM documents')[0][0];path=s.document_storage.personal.path(doc);assert path.exists()
    uid=query(database,u,'SELECT id FROM users')[0][0]
    assert 'закрыт' in erase(s,u).text
    assert 'Доступ к данным закрыт' in send(s,u,'/history').text
    assert not query(database,u,'SELECT id FROM operations')
    s.process_erasures();s.process_erasures()
    assert not path.exists()
    assert 'Личные данные удалены' in send(s,u,'/start').text
    with psycopg.connect(database[0]) as c:
        c.execute('ALTER TABLE balans.users NO FORCE ROW LEVEL SECURITY')
        assert c.execute('SELECT telegram_user_id FROM balans.users WHERE id=%s',(uid,)).fetchone()[0]!=u
        c.execute('ALTER TABLE balans.users FORCE ROW LEVEL SECURITY')
    registry=(s.receipt_storage.root.parent/'deletions.jsonl').read_text()
    assert str(u) not in registry and str(uid) in registry


def test_shared_history_survives_participant_erasure(private_service,database):
    s=private_service;owner,user=next(USERS),next(USERS)
    send(s,user,callback=draft(s,user,'777'))
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    identity=op(s,user,database);card=attach(s,user,identity)
    doc=query(database,user,'SELECT id FROM documents')[0][0];path=s.document_storage.shared.path(doc)
    erase(s,user);s.process_erasures()
    assert path.exists()
    assert '100,00' in send(s,owner,'/history').text
    assert '777' not in send(s,owner,'/history').text
    assert send(s,owner,callback=button(card,'Скачать оригинал 1')).generated_document
    assert query(database,owner,'SELECT count(*) FROM operations')==[(1,)]


def test_owner_erasure_archives_shared_budget(private_service,database):
    s=private_service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    op(s,user,database);erase(s,owner);s.process_erasures()
    assert '100,00' in send(s,user,'/history').text
    assert 'архивирован' in send(s,user,'/manual 100').text


def test_retention_off_preserves_shared_originals(private_service,database):
    s=private_service;owner,user=next(USERS),next(USERS)
    personal=op(s,user,database);attach(s,user,personal)
    file=query(database,user,'SELECT id FROM documents')[0][0]
    send(s,user,'/retention off')
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    shared=op(s,user,database);card=attach(s,user,shared)
    shared_file=query(database,user,'SELECT id FROM documents')[0][0]
    s.purge_private_files()
    assert not s.document_storage.personal.path(file).exists()
    assert s.document_storage.shared.path(shared_file).exists()
    assert send(s,owner,callback=button(card,'Скачать оригинал 1')).generated_document


def test_wrong_user_cannot_confirm_erasure(private_service):
    s=private_service;u,v=next(USERS),next(USERS)
    r=send(s,u,'/delete');send(s,v,'/start')
    assert 'истекло' in send(s,v,callback=button(r,'Подтверждаю удаление личного профиля')).text
    assert 'расход' in send(s,u,'/manual 10').text.lower() or send(s,u,'/history').text


def test_privacy_copy_matches_default_recognition(private_service):
    s=private_service;u=next(USERS)
    text=send(s,u,'/privacy').text
    assert 'включено по умолчанию' in text
    assert 'отдельное разрешение перед отправкой файла не требуется' in text
    assert 'отдельного согласия для текста' not in text
