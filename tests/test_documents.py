from itertools import count
from datetime import datetime,timezone
from uuid import UUID
import base64
import os
import time
import pytest
from test_service import send,query,draft
from test_receipts import button,receipts,review,save
from test_workspaces import admit
from receipt_fixtures import photo_bytes,pdf_bytes

USERS=count(110000000);UPDATES=count(800000)

def op(s,u,db):
    send(s,u,callback=draft(s,u,'100'))
    return str(query(db,u,'SELECT id FROM operations ORDER BY created_at DESC LIMIT 1')[0][0])

def attach(s,u,identity,data=None):
    send(s,u,'/attach operation '+identity)
    assert s.receipt_preflight(u,42,next(UPDATES)) is None
    return s.receive_receipt(u,42,next(UPDATES),datetime.now(timezone.utc),'file-upload',data or photo_bytes())

def test_attach_without_ai_or_new_expense(service,database):
    s=service;u=next(USERS);identity=op(s,u,database)
    reply=attach(s,u,identity,pdf_bytes())
    assert 'Оригинал сохранён' in reply.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    original=send(s,u,callback=button(reply,'Скачать оригинал 1'))
    assert base64.b64decode(original.generated_document).startswith(b'%PDF-')
    assert query(database,u,'SELECT count(*) FROM ai_jobs')==[(0,)]


def test_shared_retention_replacement_and_revocation(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    identity=op(s,user,database);card=attach(s,user,identity)
    doc=query(database,user,'SELECT id,permanent FROM documents')[0]
    assert doc[1]
    path=s.document_storage.shared.path(doc[0]);os.utime(path,(time.time()-40*86400,)*2)
    s.document_storage.purge()
    assert send(s,owner,callback=button(card,'Скачать оригинал 1')).generated_document
    send(s,owner,f'/review {identity} | accept')
    send(s,user,callback=button(card,'Заменить файл 1'))
    reply=s.receive_receipt(user,42,next(UPDATES),datetime.now(timezone.utc),'replacement',pdf_bytes())
    assert 'Не проверено' in reply.text
    assert query(database,owner,'SELECT state FROM documents ORDER BY created_at')==[('superseded',),('active',)]
    assert send(s,user,callback=button(card,'Скачать оригинал 1')).generated_document
    members=send(s,owner,'/members');remove=send(s,owner,callback=button(members,'Исключить '+str(user)))
    send(s,owner,callback=button(remove,'Подтвердить прекращение доступа'))
    assert not send(s,user,callback=button(card,'Скачать оригинал 1')).generated_document
    assert send(s,owner,callback=button(card,'Скачать оригинал 1')).generated_document


def test_required_document_correction_and_closed_period(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    send(s,owner,'/docpolicy on | 0')
    identity=op(s,user,database)
    assert 'Нужен документ' in send(s,owner,f'/review {identity} | accept').text
    assert query(database,owner,'SELECT sum(delta) FROM postings')[0][0]==-100
    send(s,owner,f'/review {identity} | accept | Чек потерян, исключение согласовано')
    edit=send(s,user,callback='fedit:'+identity)
    assert 'Сначала запросите' in send(s,user,callback=button(edit,'Сохранить изменения')).text
    send(s,user,f'/correction {identity} | Уточнить дату')
    send(s,owner,f'/review {identity} | approve_correction | Разрешено')
    send(s,user,callback=button(edit,'Сохранить изменения'))
    assert query(database,owner,'SELECT status FROM document_sets WHERE id=%s',(UUID(identity),))==[('unreviewed',)]
    today=datetime.now().strftime('%d.%m.%Y')
    send(s,owner,f'/periodclose {today} | {today} | Сверено')
    edit=send(s,user,callback='fedit:'+identity)
    assert 'Период закрыт' in send(s,user,callback=button(edit,'Сохранить изменения')).text
    assert 'Период закрыт' in send(s,user,'/attach operation '+identity).text
    send(s,owner,f'/periodopen {today} | {today} | Исправление')
    assert 'сохранена' in send(s,user,callback=button(edit,'Сохранить изменения')).text


def test_delete_requires_manager_and_removes_original(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    identity=op(s,user,database);card=attach(s,user,identity)
    doc=str(query(database,user,'SELECT id FROM documents')[0][0])
    send(s,user,f'/docdelete {doc} | Ошибочный файл')
    r=send(s,user,f'/docdeleteconfirm {doc} | Ошибка')
    assert 'руководитель' in send(s,user,callback=button(r,'Удалить оригинал')).text
    r=send(s,owner,f'/docdeleteconfirm {doc} | Ошибку подтверждаю')
    assert 'Оригинал удалён' in send(s,owner,callback=button(r,'Удалить оригинал')).text
    assert not s.document_storage.shared.path(doc).exists()
    assert not send(s,owner,callback=button(card,'Скачать оригинал 1')).generated_document
    assert query(database,owner,'SELECT count(*) FROM operations')==[(1,)]


def test_receipt_original_attached_automatically(receipts,database):
    s,ai,storage=receipts;u=next(USERS)
    save(s,u,review(s,u))
    assert query(database,u,'SELECT count(*) FROM documents WHERE source_receipt_id IS NOT NULL')==[(1,)]


def test_quota_failure_queue_and_corruption(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    send(s,owner,'/docquota 1 | 15')
    identity=op(s,user,database)
    assert 'Запись 1' in str(send(s,owner,'/reviewqueue document=missing').buttons)
    card=attach(s,user,identity)
    assert send(s,owner,'/reviewqueue document=missing').buttons==[]
    assert 'Файл не добавлен' in attach(s,user,identity).text
    assert query(database,user,'SELECT count(*) FROM documents')==[(1,)]
    doc=query(database,user,'SELECT id FROM documents')[0][0]
    s.document_storage.shared.path(doc).write_bytes(b'corrupt')
    reply=send(s,user,callback=button(card,'Скачать оригинал 1'))
    assert not reply.generated_document and 'Контрольная сумма' in reply.text


def test_active_pdf_is_rejected(service,database):
    from io import BytesIO
    from pypdf import PdfWriter
    s=service;u=next(USERS);identity=op(s,u,database)
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.add_js('app.alert("test")')
    buf=BytesIO();writer.write(buf)
    reply=attach(s,u,identity,buf.getvalue())
    assert 'Не удалось открыть файл' in reply.text
    assert query(database,u,'SELECT count(*) FROM documents')==[(0,)]


def test_new_attachment_does_not_unlock_accepted_amount(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    identity=op(s,user,database)
    send(s,owner,f'/review {identity} | accept')
    attach(s,user,identity)
    edit=send(s,user,callback='fedit:'+identity)
    assert 'Сначала запросите' in send(s,user,callback=button(edit,'Сохранить изменения')).text


def test_long_pdf_attachment_and_cached_download_reauthorization(service,database):
    from io import BytesIO
    from pypdf import PdfWriter
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    identity=op(s,user,database)
    w=PdfWriter()
    for _ in range(11):w.add_blank_page(width=100,height=100)
    b=BytesIO();w.write(b)
    card=attach(s,user,identity,b.getvalue())
    callback=button(card,'Скачать оригинал 1');update=next(UPDATES)
    assert send(s,user,callback=callback,update=update).generated_document
    members=send(s,owner,'/members');r=send(s,owner,callback=button(members,'Исключить '+str(user)))
    send(s,owner,callback=button(r,'Подтвердить прекращение доступа'))
    assert not send(s,user,callback=callback,update=update).generated_document
