from itertools import count
import pytest
from test_service import send,query,NOW
from test_receipts import receipts,review,button
from test_voice import voices,upload
from test_media_flow import extracted,transaction,category
from receipt_fixtures import photo_bytes,pdf_bytes

USERS=count(330000000)

class ParsedTextFake:
    available=True;model='test-text'
    def __init__(self,service):self.service=service
    def extract(self,request):
        from balans.text_input import parse_items
        from balans.text_ai import TextResult
        from balans.voice_ai import VoiceResult
        from datetime import datetime,timezone
        try:items=parse_items(request['message'],datetime.fromisoformat(request['message_date']).replace(tzinfo=timezone.utc),'UTC')
        except ValueError:return TextResult(operations=[]),10,10
        rows=[]
        for item in items:
            category=None
            if self.service.ai.available:
                category=self.service.ai.classify({'description':item['description'],'categories':request['categories']}).category_id
            rows.append(VoiceResult(kind=item['kind'],amount=item['amount'],amount_confidence=1,currency=None,description=item['description'],occurred_on=None if item['capture_warnings'] else item['date'],date_note='',category_id=category,category_confidence=1))
        return TextResult(operations=rows),10,10

def enable(s,u):
    send(s,u,'/start');send(s,u,callback='usercurrency:RUB')
    send(s,u,callback='captureon');send(s,u,'/ai on');s.text_ai=ParsedTextFake(s)

def check(card):
    assert card.text.startswith('✅ Расход записан'),card.text
    assert len(card.buttons)==1 and len(card.buttons[0])==1
    assert button(card,'✏️ Изменить').startswith('fedit:')
    assert 'Описание:' not in card.text

def test_text_auto_and_replay(service,database):
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Вода 339',update=339000001);check(card)
    assert '⚠️ Без категории' in card.text
    assert send(s,u,'Вода 339',update=339000001).text==card.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    check(send(s,u,'Вода 339'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]

def test_multiple_text(service,database):
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Вода 339\nХлеб 50',update=339000002);check(card)
    assert len(card.additional_replies)==1
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]
    assert send(s,u,'Вода 339\nХлеб 50',update=339000002).text==card.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]

@pytest.mark.parametrize('source',['photo','pdf'])
def test_receipt_auto_and_exact_reupload(receipts,database,source):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    data=photo_bytes() if source=='photo' else pdf_bytes()
    card=review(s,u,data);check(card)
    assert '🏷 Продукты' in card.text
    again=review(s,u,data)
    assert 'уже записан' in again.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]

def test_receipt_payment_clarification(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u);ai.changes={'payment_status':'unknown'}
    card=review(s,u)
    assert not query(database,u,'SELECT id FROM operations')
    check(send(s,u,callback=button(card,'Да, оплачена')))

def test_voice_missing_fields_and_edit(voices,database):
    s,ai=voices;u=next(USERS);enable(s,u)
    ai.changes={'amount':None,'occurred_on':None,'category_id':None}
    card=upload(s,u)
    assert 'определить сумму' in card.text
    assert not query(database,u,'SELECT id FROM operations')
    card=send(s,u,'850');check(card)
    assert '⚠️ Без категории' in card.text and 'дата сообщения' in card.text
    assert query(database,u,'SELECT capture_warnings FROM operation_revisions')==[(['date'],)]
    edit=send(s,u,callback=button(card,'✏️ Изменить'))
    send(s,u,callback=button(edit,'Изменить дату'));edit=send(s,u,'13.09.2026')
    saved=edit;check(saved)
    assert 'дата сообщения' not in saved.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT capture_warnings FROM operation_revisions ORDER BY revision_no')==[(['date'],),([],)]

def test_voice_auto(voices,database):
    s,ai=voices;u=next(USERS);enable(s,u);check(upload(s,u))
    assert query(database,u,'SELECT source_kind FROM operation_revisions')==[('voice',)]

def test_media_multiple_auto(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u);cat=category(database,u)
    card=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')]);check(card)
    assert len(card.additional_replies)==1
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]
    assert query(database,u,'SELECT state FROM media_queues')==[('done',)]
    again=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')])
    assert 'уже записан' in again.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]

from test_categorization import ai_service

@pytest.mark.parametrize('error',[None,'timeout'])
def test_text_ai_and_failed_category_fallback(ai_service,database,error):
    s,ai=ai_service;u=next(USERS);enable(s,u);send(s,u,'/ai on');ai.error=error
    card=send(s,u,'Кофе 250 вчера\nБулочка 80 вчера');check(card)
    assert len(card.additional_replies)==1 and len(ai.calls)==2
    assert ('⚠️ Без категории' if error else '🏷 Кафе и рестораны') in card.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]

def test_media_unknown_category_and_date(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    item=transaction(None);item['occurred_on']=None
    card=extracted(s,ai,u,[item]);check(card)
    assert '⚠️ Без категории' in card.text
    assert '📅 '+NOW.strftime('%d.%m.%Y') in card.text
    assert 'дата сообщения' not in card.text
    assert query(database,u,'SELECT occurred_on,capture_warnings FROM operation_revisions')==[(NOW.date(),[])]

def test_receipt_missing_amount(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    ai.changes={'total':None,'items':[],'items_complete':False}
    card=review(s,u)
    assert not query(database,u,'SELECT id FROM operations')
    assert 'сумму' in card.text
    check(send(s,u,'150'))

def test_no_autosave_planned_text(service,database):
    s=service;u=next(USERS);enable(s,u)
    send(s,u,'Хочу купить кофе 250 завтра')
    assert not query(database,u,'SELECT id FROM operations')

def test_multiple_cards_are_delivered_with_individual_edit_buttons(service,database):
    import asyncio
    from balans.__main__ import deliver_reply
    from test_telegram import FakeBot
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Вода 339 вчера\nХлеб 50 вчера')
    bot=FakeBot();asyncio.run(deliver_reply(bot,u,card,s))
    assert len(bot.messages)==2
    targets=[]
    for _,text,options in bot.messages:
        assert text.startswith('✅ Расход записан')
        keyboard=options['reply_markup'].inline_keyboard
        assert len(keyboard)==1 and len(keyboard[0])==1
        assert keyboard[0][0].text=='✏️ Изменить'
        targets.append(keyboard[0][0].callback_data)
    assert len(set(targets))==2

def test_cancelled_media_can_be_recorded_again(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u);cat=category(database,u)
    card=extracted(s,ai,u,[transaction(cat)])
    identity=button(card,'✏️ Изменить').split(':')[1]
    send(s,u,callback='fdelete:'+identity)
    preview=send(s,u,'Ошибочная запись')
    send(s,u,callback=button(preview,'Сохранить изменения'))
    check(extracted(s,ai,u,[transaction(cat)]))
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(1,)]
    assert 'уже записан' in extracted(s,ai,u,[transaction(cat)]).text
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(1,)]
