from itertools import count
from datetime import datetime,timezone
import pytest
from balans.text_ai import TextResult
from balans.voice_ai import VoiceResult
from test_service import send,query
from test_receipts import receipts,button
from test_voice import voices,upload
from test_media_flow import extracted,transaction,category

USERS=count(350000000)

class TextFake:
    available=True;model='test-text'
    def __init__(self,kind='income',amount='5000'):
        self.calls=[];self.kind=kind;self.amount=amount;self.fail=False
    def extract(self,request):
        self.calls.append(request)
        if self.fail:raise TimeoutError()
        return TextResult(operations=[VoiceResult(kind=self.kind,amount=self.amount,amount_confidence=.99,currency=None,description='Поступление',occurred_on=None,date_note='',category_id=None,category_confidence=0)]),100,20

def enable(s,u):
    send(s,u,'/start');send(s,u,callback='usercurrency:RUB');send(s,u,callback='captureon');send(s,u,'/ai on')

@pytest.mark.parametrize('kind',['income','opening'])
def test_text_ai_money(service,database,kind):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake(kind)
    card=send(s,u,'произвольная фраза без ключевых слов',update=next(USERS))
    assert 'записан' in card.text,card.text
    assert query(database,u,'SELECT kind FROM operations')==[(kind,)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(5000,)]
    assert len(s.text_ai.calls)==1

@pytest.mark.parametrize('selected',['income','opening'])
def test_ambiguous_text_two_buttons_and_replay(service,database,selected):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake('incoming');update=next(USERS)
    card=send(s,u,'Внёс пять тысяч',update=update)
    assert [x for row in card.buttons for x,_ in row]==['Доход','Начальный остаток']
    assert not query(database,u,'SELECT id FROM operations')
    send(s,u,callback=button(card,'Доход' if selected=='income' else 'Начальный остаток'))
    assert query(database,u,'SELECT kind FROM operations')==[(selected,)]
    send(s,u,'Внёс пять тысяч',update=update)
    assert len(s.text_ai.calls)==1
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]

@pytest.mark.parametrize('kind',['income','opening','incoming'])
def test_voice_money(voices,database,kind):
    s,ai=voices;u=next(USERS);enable(s,u);ai.changes={'kind':kind}
    card=upload(s,u)
    if kind=='incoming':card=send(s,u,callback=button(card,'Доход'))
    assert 'записан' in card.text,card.text
    assert query(database,u,'SELECT kind FROM operations')==[('income' if kind=='incoming' else kind,)]
    assert query(database,u,'SELECT source_kind FROM operation_revisions')==[('voice',)]

@pytest.mark.parametrize('kind',['income','opening','incoming'])
@pytest.mark.parametrize('source',['receipt','screenshot'])
def test_image_money(receipts,database,kind,source):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    card=extracted(s,ai,u,[transaction(None,kind=kind)],source=source)
    if kind=='incoming':card=send(s,u,callback=button(card,'Начальный остаток'))
    assert 'записан' in card.text,card.text
    assert query(database,u,'SELECT kind FROM operations')==[('opening' if kind=='incoming' else kind,)]

def test_failed_text_no_save_or_charge(service,database):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake();s.text_ai.fail=True
    card=send(s,u,'Внёс пять тысяч')
    assert 'Не удалось' in card.text
    assert not query(database,u,'SELECT id FROM operations')
    assert query(database,u,'SELECT state FROM quota_reservations') in ([],[('released',)])

@pytest.mark.parametrize('kind',['income','opening'])
def test_missing_text_amount_then_new_message(service,database,kind):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake(kind,None)
    assert 'сумму' in send(s,u,'Поступление').text
    card=send(s,u,'1000');assert 'записан' in card.text,card.text
    s.text_ai.amount='2000';s.text_ai.kind='income'
    assert 'записан' in send(s,u,'Ещё поступление').text
    assert query(database,u,'SELECT count(*) FROM operations')==[(2,)]

@pytest.mark.parametrize('kind',['income','opening'])
def test_missing_voice_amount(voices,database,kind):
    s,ai=voices;u=next(USERS);enable(s,u);ai.changes={'kind':kind,'amount':None}
    assert 'сумму' in upload(s,u).text
    card=send(s,u,'5000');assert 'записан' in card.text,card.text
    assert query(database,u,'SELECT kind FROM operations')==[(kind,)]

def test_opening_excluded_from_income_report(service,database):
    from balans.report_data import summarize
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake('opening')
    send(s,u,'На начало учёта было пять тысяч')
    report=send(s,u,'/report')
    assert 'Доходы — 0,00' in report.text,report.text

def test_text_adapter_structured_request():
    import json,httpx
    from openai import OpenAI
    from balans.text_ai import TextAI
    from test_ai import api_response
    payload=TextFake('opening').extract({'categories':[]})[0].model_dump()
    def handler(request):
        body=json.loads(request.content)
        assert body['store'] is False
        assert body['text']['format']['type']=='json_schema'
        assert 'incoming' in str(body['text']['format']['schema'])
        return httpx.Response(200,json=api_response(content=[{'type':'output_text','text':json.dumps(payload),'annotations':[]}]))
    client=OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        result,inp,out=TextAI(client,'test').extract({'message':'Начинаю учёт с 5000','message_date':'2026-09-13','categories':[],'user_currency':'RUB'})
        assert result.operations[0].kind=='opening' and inp==100 and out==20
    finally:client.close()

def test_erasure_includes_text_jobs(service,database):
    from test_privacy import erase
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake()
    send(s,u,'Зарплата 5000');assert query(database,u,'SELECT count(*) FROM text_jobs')==[(1,)]
    erase(s,u);s.process_erasures()
    assert 'Личные данные удалены' in send(s,u,'/start').text


def test_cancel_during_text_ai_prevents_save(service,database):
    s=service;u=next(USERS);enable(s,u)
    class Cancelling(TextFake):
        def extract(self,request):
            send(s,u,'/cancel')
            return super().extract(request)
    s.text_ai=Cancelling();card=send(s,u,'Внёс 5000')
    assert 'отменено' in card.text
    assert not query(database,u,'SELECT id FROM operations')
    assert query(database,u,'SELECT state FROM text_jobs')==[('cancelled',)]


def test_opening_cannot_be_added_twice(service,database):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake('opening')
    send(s,u,'Начальный остаток 5000')
    assert 'уже задан' in send(s,u,'Начальный остаток 5000').text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]

def test_pdf_money(receipts,database):
    from receipt_fixtures import pdf_bytes
    from test_receipts import review
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    ai.changes={'source_type':'receipt','document_kind':'multiple','transactions':[transaction(None,kind='income')]}
    card=review(s,u,pdf_bytes())
    assert 'Доход записан' in card.text
    assert query(database,u,'SELECT kind FROM operations')==[('income',)]


def test_explicit_other_currency_is_not_relabelled(service,database):
    s=service;u=next(USERS);enable(s,u)
    class Foreign(TextFake):
        def extract(self,request):
            result,inp,out=super().extract(request);result.operations[0].currency='USD';return result,inp,out
    s.text_ai=Foreign();assert 'Валюта' in send(s,u,'Поступило 5000 долларов').text
    assert not query(database,u,'SELECT id FROM operations')


def test_mixed_text_asks_then_continues(service,database):
    s=service;u=next(USERS);enable(s,u)
    class Mixed(TextFake):
        def extract(self,request):
            result,inp,out=super().extract(request)
            result.operations.append(result.operations[0].model_copy(update={'kind':'income','amount':'1000'}))
            return result,inp,out
    s.text_ai=Mixed('incoming');card=send(s,u,'Внёс 5000, ещё получил 1000')
    assert not query(database,u,'SELECT id FROM operations')
    send(s,u,callback=button(card,'Начальный остаток'))
    send(s,u,callback='tqueue')
    assert query(database,u,'SELECT kind FROM operations ORDER BY created_at')==[('opening',),('income',)]
    assert len(s.text_ai.calls)==1
