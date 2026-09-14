from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count
from threading import Event
from uuid import UUID

import psycopg
import pytest

from balans.receipt_ai import ReceiptExtraction,ReceiptResult,validate_receipt
from balans.receipt_media import ReceiptStorage
from balans.service import Service
from receipt_fixtures import receipt_payload,photo_bytes,pdf_bytes,scan_pdf_bytes

IDS=count(200000);USERS=count(40000000)
NOW=datetime(2026,9,10,12,tzinfo=timezone.utc)


class ReceiptFake:
    available=True
    model='test'
    def __init__(self):
        self.calls=[];self.changes={};self.error=None
    def extract(self,prepared,categories):
        self.calls.append(prepared)
        if self.error:
            return ReceiptExtraction(error_code=self.error)
        category=next(c['id'] for c in categories if c['name']=='Продукты')
        payload=receipt_payload(category,**self.changes)
        return ReceiptExtraction(validate_receipt(ReceiptResult(**payload),categories),response_id='resp_test',input_tokens=100,output_tokens=200)


@pytest.fixture
def receipts(database,tmp_path):
    ai=ReceiptFake();storage=ReceiptStorage(tmp_path/'originals')
    service=Service(database[1],receipt_ai=ai,receipt_storage=storage)
    yield service,ai,storage
    service.close()


def send(s,user,text='',callback=None,update=None):
    return s.handle(user,42,next(IDS) if update is None else update,text,NOW,callback)


def button(reply,label):
    return next(data for row in reply.buttons for title,data in row if title==label)


def query(database,user,statement,args=()):
    with psycopg.connect(database[1]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        return c.execute(statement,args).fetchall()


def upload(s,user,data=None,update=None,kind='document'):
    send(s,user,callback='receipts_on')
    return s.receive_receipt(user,42,next(IDS) if update is None else update,NOW,'file-test',data or photo_bytes(),kind)


def review(s,user,data=None,kind='document'):
    collection=upload(s,user,data,kind=kind)
    return send(s,user,callback=button(collection,'Распознать один чек'))


def save(s,user,card):
    card=send(s,user,callback=button(card,'Да, покупка оплачена'))
    return send(s,user,callback=button(card,'Сохранить расход'))


@pytest.mark.parametrize('data,kind',[(pdf_bytes(),'document'),(scan_pdf_bytes(),'document'),(photo_bytes(),'photo')])
def test_receipt_to_postings_and_original(receipts,database,data,kind):
    s,ai,storage=receipts;user=next(USERS)
    card=review(s,user,data,kind)
    assert 'Итог: 150,00' in card.text and 'Milk' in card.text
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    draft_id=query(database,user,'SELECT id FROM operation_drafts')[0][0]
    with pytest.raises(psycopg.errors.RaiseException):
        query(database,user,'SELECT save_expense(%s)',(draft_id,))
    saved=save(s,user,card)
    assert query(database,user,'SELECT count(*),sum(delta) FROM postings')==[(1,Decimal('-150'))]
    assert query(database,user,'SELECT count(*) FROM receipt_items')==[(2,)]
    assert query(database,user,'SELECT merchant,source_kind FROM operation_revisions')==[('TEST SHOP','receipt')]
    source=send(s,user,callback=button(saved,'Чек'))
    assert (source.photo_ids if kind=='photo' else source.document_ids)==['file-test']
    assert 'Milk' in send(s,user,callback=button(saved,'Позиции')).text
    assert len(ai.calls)==1
    assert len(list(storage.root.glob('*.bin')))==1
    history=send(s,user,'/history')
    send(s,user,callback=button(history,'✏️ Изменить расход'))
    selected=send(s,user,'1')
    assert button(selected,'Чек')


def test_enabled_by_default_and_input_gate(receipts,database):
    s,ai,_=receipts;user=next(USERS)
    send(s,user,'/ai on')
    response=s.receipt_preflight(user,42,next(IDS))
    assert response is None
    assert not ai.calls
    send(s,user,callback='receipts_on')
    send(s,user,'/manual 50')
    response=s.receipt_preflight(user,42,next(IDS))
    assert 'текущий расход' in response.text
    assert query(database,user,'SELECT count(*) FROM receipt_files')==[(0,)]


def test_upload_and_ocr_retry_idempotency(receipts,database):
    s,ai,_=receipts;user=next(USERS);update=next(IDS)
    collection=upload(s,user,update=update)
    assert s.receive_receipt(user,42,update,NOW,'file-test',photo_bytes()).text==collection.text
    callback=button(collection,'Распознать один чек');processing_update=next(IDS)
    first=send(s,user,callback=callback,update=processing_update)
    assert send(s,user,callback=callback,update=processing_update).text==first.text
    assert send(s,user,callback=callback).text==first.text
    assert len(ai.calls)==1
    assert query(database,user,'SELECT count(*) FROM receipt_files')==[(1,)]
    assert query(database,user,'SELECT count(*) FROM operation_drafts')==[(1,)]


def test_multi_page_collection_and_stale_buttons(receipts,database):
    s,ai,_=receipts;user=next(USERS)
    first=upload(s,user,photo_bytes())
    second=upload(s,user,photo_bytes(True))
    assert query(database,user,'SELECT sum(page_count) FROM receipt_files')==[(2,)]
    assert 'Состав чека изменился' in send(s,user,callback=button(first,'Распознать один чек')).text
    third=upload(s,user,photo_bytes())
    assert 'уже добавлен' in third.text
    card=send(s,user,callback=button(second,'Распознать один чек'))
    assert 'Продавец:' in card.text
    assert ai.calls[0].pages==2
    assert query(database,user,'SELECT count(*) FROM receipt_files')==[(2,)]


def test_missing_fields_are_requested(receipts,database):
    s,ai,_=receipts;user=next(USERS)
    ai.changes={'total':None,'currency':None,'occurred_on':None,'category_id':None}
    # Avoid duplicate keyword in fixture helper for category override.
    ai.changes.pop('category_id')
    card=review(s,user)
    assert 'введите сумму' in card.text
    after_amount=send(s,user,'123,45')
    assert 'Дата: '+NOW.strftime('%d.%m.%Y') in after_amount.text
    assert 'Продавец:' in after_amount.text
    saved=save(s,user,after_amount)
    assert '123,45' in saved.text
    assert query(database,user,'SELECT amount,occurred_on,capture_warnings FROM operation_revisions')==[(Decimal('123.45'),NOW.date(),[])]


def test_missing_date_uses_upload_date_without_warning(receipts,database):
    s,ai,_=receipts;user=next(USERS)
    ai.changes={'occurred_on':None}
    send(s,user,callback='captureon')
    card=review(s,user)
    assert card.text.startswith('✅ Расход записан')
    assert '📅 '+NOW.strftime('%d.%m.%Y') in card.text
    assert 'дата сообщения' not in card.text
    assert query(database,user,'SELECT occurred_on,capture_warnings FROM operation_revisions')==[(NOW.date(),[])]


def test_edit_and_duplicate_guard(receipts,database):
    s,ai,_=receipts;user=next(USERS)
    first=review(s,user)
    edit=send(s,user,callback=button(first,'Сумма'))
    assert 'введите сумму' in edit.text
    updated=send(s,user,'150')
    assert 'устарела' in send(s,user,callback=button(first,'Да, покупка оплачена')).text
    save(s,user,updated)
    again=review(s,user)
    assert 'той же суммой' in again.text
    paid=send(s,user,callback=button(again,'Да, покупка оплачена'))
    assert all(title!='Сохранить расход' for row in paid.buttons for title,_ in row)
    d=query(database,user,"SELECT id FROM operation_drafts WHERE state='pending'")[0][0]
    with pytest.raises(psycopg.errors.RaiseException):
        query(database,user,'SELECT save_expense(%s)',(d,))
    distinct=send(s,user,callback=button(paid,'Это другая покупка'))
    send(s,user,callback=button(distinct,'Сохранить расход'))
    assert query(database,user,'SELECT count(*) FROM operations')==[(2,)]


@pytest.mark.parametrize('changes',[{'document_kind':'multiple'},{'document_kind':'refund'},{'currency':'USD'}])
def test_wrong_documents_not_saved(receipts,database,changes):
    s,ai,_=receipts;user=next(USERS);ai.changes=changes
    review(s,user)
    assert query(database,user,'SELECT count(*) FROM operation_drafts')==[(0,)]
    assert query(database,user,'SELECT state FROM receipt_batches')==[('failed',)]


def test_provider_failure_explicit_retry(receipts,database):
    s,ai,_=receipts;user=next(USERS);ai.error='APITimeoutError'
    failed=review(s,user)
    assert 'Не удалось' in failed.text
    assert query(database,user,'SELECT count(*) FROM operation_drafts')==[(0,)]
    ai.error=None
    ready=send(s,user,callback=button(failed,'Повторить распознавание'))
    assert 'Продавец:' in ready.text
    assert len(ai.calls)==2


@pytest.mark.parametrize('action',['/cancel'])
def test_late_ocr_cancelled(receipts,database,action):
    s,ai,_=receipts;user=next(USERS)
    collection=upload(s,user);entered=Event();release=Event();original=ai.extract
    def delayed(*args):
        entered.set();assert release.wait(5);return original(*args)
    ai.extract=delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(send,s,user,callback=button(collection,'Распознать один чек'))
        assert entered.wait(5)
        try:
            send(s,user,action)
        finally:
            release.set()
        assert 'отменена' in future.result().text
    assert query(database,user,'SELECT count(*) FROM operation_drafts')==[(0,)]


def test_rls_sources_and_items(receipts,database):
    s,_,_=receipts;alice,bob=next(USERS),next(USERS)
    saved=save(s,alice,review(s,alice))
    send(s,bob,'/start')
    assert 'недоступен' in send(s,bob,callback=button(saved,'Чек')).text
    for table in ('receipt_files','receipt_batches','receipt_items'):
        assert query(database,bob,f'SELECT count(*) FROM {table}')==[(0,)]
    assert 'недоступен' in send(s,bob,callback=button(saved,'Позиции')).text


def test_category_correction_preserves_receipt_metadata(receipts,database):
    s,_,_=receipts;user=next(USERS)
    saved=save(s,user,review(s,user))
    categories=send(s,user,callback=button(saved,'Исправить категорию'))
    send(s,user,callback=button(categories,'Дом'))
    assert query(database,user,'SELECT merchant,source_kind FROM operation_revisions ORDER BY revision_no')==[('TEST SHOP','receipt'),('TEST SHOP','receipt')]
    assert query(database,user,'SELECT count(*) FROM postings')==[(1,)]
    assert query(database,user,'SELECT count(*) FROM receipt_items')==[(2,)]


def test_expired_source_not_replayed_from_update_cache(receipts,database):
    s,_,_=receipts;user=next(USERS)
    saved=save(s,user,review(s,user))
    update=next(IDS);callback=button(saved,'Чек')
    assert send(s,user,callback=callback,update=update).document_ids
    with psycopg.connect(database[0]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute("UPDATE balans.receipt_files SET expires_at=now()-interval '1 second'")
    result=send(s,user,callback=callback,update=update)
    assert not result.document_ids and not result.photo_ids


def test_interrupted_processing_no_automatic_paid_retry(receipts,database):
    s,ai,_=receipts;user=next(USERS);collection=upload(s,user)
    def crash(*args):
        ai.calls.append('interrupted')
        raise SystemExit('simulated crash')
    ai.extract=crash
    callback=button(collection,'Распознать один чек');update=next(IDS)
    with pytest.raises(SystemExit):
        send(s,user,callback=callback,update=update)
    query(database,user,"UPDATE receipt_batches SET lease_until=now()-interval '1 second' RETURNING id")
    reply=send(s,user,callback=callback,update=update)
    assert 'Не удалось' in reply.text
    assert len(ai.calls)==1
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
