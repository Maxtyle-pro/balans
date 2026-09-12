from itertools import count
from decimal import Decimal
from uuid import UUID
from test_service import send,query,draft,NOW
from zoneinfo import ZoneInfo
from receipt_fixtures import photo_bytes
from test_receipts import receipts,review,button
from test_workspaces import admit

USERS=count(120000000);MEDIA_UPDATES=count(2100000)

def transaction(category,amount='150',kind='expense',status='paid',**changes):
    return dict(kind=kind,amount=amount,currency='RUB',occurred_on=NOW.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),merchant='Магазин',description='Покупка',category_id=category,payment_status=status,confidence_amount=.99,account_hint='Карта **** 1234',transaction_reference='operation-unique-'+amount,items=[],**changes)

def extracted(s,ai,u,items,source='screenshot'):
    send(s,u,'/start')
    ai.changes={'source_type':source,'transactions':items,'document_kind':'multiple'}
    send(s,u,callback='receipts_on')
    collection=s.receive_receipt(u,42,next(MEDIA_UPDATES),NOW,'media-fixture',photo_bytes())
    return send(s,u,callback=button(collection,'Распознать один чек'))

def category(db,u):return str(query(db,u,"SELECT id FROM categories WHERE name='Продукты'")[0][0])

def open_row(s,u,reply,index=1):
    if reply.text.startswith('Операция '):return reply
    return send(s,u,callback=next(data for row in reply.buttons for _,data in row if data.startswith('mreview:') and data.split(':')[2]==str(index-1)))

def paid(s,u,card):return send(s,u,callback=button(card,'Операция совершена'))

def save_row(s,u,card):return send(s,u,callback=button(card,'Сохранить эту строку'))

def test_list_separate_confirmation_sources_and_originals(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    listing=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')])
    assert 'Операции на изображении' in listing.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    first=paid(s,u,open_row(s,u,listing));save_row(s,u,first)
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    listing=send(s,u,'/media');save_row(s,u,paid(s,u,open_row(s,u,listing,2)))
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-400'),)]
    assert query(database,u,'SELECT DISTINCT source_kind FROM operation_revisions')==[('screenshot',)]
    assert query(database,u,'SELECT count(*) FROM documents')==[(2,)]
    assert 'Повторных записей нет' in save_row(s,u,first).text


def test_unpaid_terminal_and_missing_date(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    row=transaction(cat,status='pending');row['occurred_on']=None
    card=open_row(s,u,extracted(s,ai,u,[row],source='terminal'))
    assert not any(label=='Сохранить эту строку' for buttons in card.buttons for label,_ in buttons)
    q=query(database,u,'SELECT id,version FROM media_queues')[0]
    assert 'Подтвердите' in send(s,u,callback=f'msave:{q[0]}:0:{q[1]}').text
    send(s,u,callback=button(card,'Дата'));card=send(s,u,'сегодня')
    card=send(s,u,callback=button(card,'Ещё не совершена'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    save_row(s,u,paid(s,u,card))
    assert query(database,u,'SELECT source_kind FROM operation_revisions')==[('terminal',)]


def test_duplicate_can_attach_to_existing_expense(receipts,database):
    s,ai,_=receipts;u=next(USERS)
    send(s,u,callback=draft(s,u,'150'));cat=category(database,u)
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat)]))
    assert 'Возможный дубль' in card.text
    link=next(data for row in card.buttons for label,data in row if label.startswith('Прикрепить к'))
    preview=send(s,u,callback=link)
    send(s,u,callback=button(preview,'Прикрепить без нового расхода'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT count(*) FROM documents')==[(1,)]


def test_shared_income_is_unreconciled_claim(receipts,database):
    s,ai,_=receipts;owner,u=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,u);cat=category(database,u)
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat,'2000',kind='income')]))
    assert 'ожидающий сверки' in card.text
    save_row(s,u,paid(s,u,card))
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    assert query(database,owner,'SELECT state FROM fund_claims')==[('pending',)]
    assert 'заявленный: 2 000,00' in send(s,u,'/accounts').text
    assert query(database,owner,'SELECT permanent FROM documents')==[(True,)]


def test_reference_masking_and_hash_survives_category_change(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    row=transaction(cat);row['merchant']='Карта 1234 5678 9012 3456';row['transaction_reference']='visible-transfer-id'
    card=open_row(s,u,extracted(s,ai,u,[row]))
    assert '1234 5678' not in card.text
    save_row(s,u,paid(s,u,card))
    operation,reference=query(database,u,'SELECT operation_id,external_reference_hash FROM operation_revisions')[0]
    assert len(reference)==64 and reference!='visible-transfer-id'
    menu=send(s,u,callback='opcat:'+str(operation));send(s,u,callback=button(menu,'Дом'))
    assert query(database,u,'SELECT DISTINCT external_reference_hash FROM operation_revisions')==[(reference,)]


def test_transfer_and_refund_need_explicit_linked_accounts(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    send(s,u,'/account Наличные')
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat,'500',kind='transfer')]))
    send(s,u,callback=button(card,'Счёт получателя'));card=send(s,u,'Наличные')
    save_row(s,u,paid(s,u,card))
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal(0),)]
    send(s,u,callback=draft(s,u,'150'))
    purchase=str(query(database,u,"SELECT id FROM operations WHERE kind='expense'")[0][0])
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat,'50',kind='refund')]))
    send(s,u,callback=button(card,'Исходная покупка'));card=send(s,u,purchase)
    save_row(s,u,paid(s,u,card))
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-100'),)]


def test_stale_media_button_and_manual_cross_source_warning(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    card=paid(s,u,open_row(s,u,extracted(s,ai,u,[transaction(cat)])))
    old=button(card,'Сохранить эту строку')
    send(s,u,callback=button(card,'Сумма'));card=send(s,u,'151')
    assert 'устарела' in send(s,u,callback=old).text
    save_row(s,u,paid(s,u,card))
    send(s,u,'/manual 151');send(s,u,'Продукты');send(s,u,'Повтор');card=send(s,u,'сегодня')
    assert 'Возможный дубль' in card.text


def test_terminal_legacy_shape_and_unknown_source_are_reviewable(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start')
    ai.changes={'source_type':'terminal','payment_status':'unknown'}
    listing=review(s,u)
    card=open_row(s,u,listing)
    assert 'Milk' in card.text
    send(s,u,'/media cancel')
    row=transaction(category(database,u))
    card=open_row(s,u,extracted(s,ai,u,[row],source='unknown'))
    assert 'Выберите тип изображения' in card.text
    card=send(s,u,callback=button(card,'Скриншот'))
    assert 'Выберите тип изображения' not in card.text
    save_row(s,u,paid(s,u,card))
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]


def test_media_category_correction_can_be_learned(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat)]))
    send(s,u,callback=button(card,'Выбрать другую категорию'));card=send(s,u,'Дом')
    saved=save_row(s,u,paid(s,u,card));send(s,u,callback=button(saved,'Запомнить для меня'))
    assert query(database,u,'SELECT count(*) FROM category_rules WHERE enabled')==[(1,)]
    card=open_row(s,u,extracted(s,ai,u,[transaction(cat,'250')]))
    assert 'Категория: Дом' in card.text


def test_skipped_list_has_clear_empty_state(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    listing=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')])
    listing=send(s,u,callback=button(listing,'Пропустить 1'))
    assert button(listing,'✏️ Открыть операцию').startswith('mreview:')
    done=send(s,u,callback=button(listing,'Пропустить'))
    assert 'ничего не сохранено' in done.text and 'Итого:' not in done.text
    assert button(done,'➕ Добавить расход')=='add'
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]


def test_finished_list_reports_saved_count(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    listing=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')])
    save_row(s,u,paid(s,u,open_row(s,u,listing)))
    listing=send(s,u,'/media')
    done=send(s,u,callback=button(listing,'Пропустить'))
    assert 'Сохранено операций: 1' in done.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
