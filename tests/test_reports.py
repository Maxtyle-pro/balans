from datetime import date,datetime,timezone
from decimal import Decimal
from io import BytesIO
from itertools import count
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import base64
import csv
import pytest
from pypdf import PdfReader
from balans.service import Service
from balans.report_data import period,summarize,csv_bytes
from balans.report_ai import AnalysisResult
from balans.report_pdf import render_pdf
from scripts.check_report_pdf import fixture
from test_receipts import send,button,query

USERS=count(71000000)


class FakeAnalysis:
    available=True;model='test'
    def __init__(self):self.calls=[];self.fail=False
    def analyze(self,snapshot):
        self.calls.append(snapshot)
        if self.fail:raise TimeoutError()
        return AnalysisResult(observations=['В записях преобладают продукты.'],recommendations=['Составляйте список покупок заранее.'],limitation='Доходы и полнота учёта неизвестны.')


class FakeSheets:
    available=True;email='bot@example.test'
    def __init__(self):self.valid=True;self.verifications=0;self.calls=[]
    def close(self):pass
    def verify(self,*args):self.verifications+=1;return self.valid
    def export(self,*args):
        self.calls.append(args)
        return 'https://docs.google.com/spreadsheets/d/test/edit#gid=1'


@pytest.fixture
def reporting(database):
    ai=FakeAnalysis();sheets=FakeSheets();s=Service(database[1],report_ai=ai,sheets=sheets)
    yield s,ai,sheets
    s.close()


def expense(s,user,value='100.25',description='Продукты'):
    card=send(s,user,'/manual '+value)
    send(s,user,callback=card.buttons[0][0][1])
    send(s,user,description)
    card=send(s,user,'сегодня')
    return send(s,user,callback=button(card,'Сохранить'))


def connect(s,user):
    card=send(s,user,'/sheets connect https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit')
    assert 'Баланс-доступ' in card.text
    return send(s,user,callback=button(card,'Проверить подключение'))


def test_periods_and_boundaries():
    today=date(2026,9,10)
    assert period('',today)==(date(2026,9,1),today,date(2026,8,1),date(2026,8,10))
    assert period('неделя',today)[:2]==(date(2026,9,7),today)
    assert period('вчера',today)[:2]==(date(2026,9,9),date(2026,9,9))
    assert period('2026-02',today)[1]==date(2026,2,28)
    for text in ('2026-13','2027-01','10.09.2026 01.09.2026','01.01.2020 01.01.2026','bad','01.01.0001 10.01.0001'):
        with pytest.raises(ValueError):period(text,today)


def test_exact_arithmetic_and_zero_days():
    rows=[{'amount':'0.10','category':'Дом','date':'2026-09-01'},{'amount':'0.20','category':'Дом','date':'2026-09-01'}]
    summary=summarize(rows,date(2026,9,1),date(2026,9,2))
    assert summary['total']=='0.30' and summary['daily_average']=='0.15'
    assert summary['days'][1]['total']=='0'


def test_snapshot_current_revisions_and_isolation(reporting,database):
    s,ai,_=reporting;user=next(USERS);other=next(USERS)
    expense(s,user);expense(s,user,'200.35')
    card=send(s,user,'/report')
    assert '300,60' in card.text
    assert 'не вычисляется' not in card.text
    assert query(database,other,'SELECT * FROM reports')==[]
    assert 'недоступен' in send(s,other,callback=button(card,'📄 Скачать PDF-отчёт')).text
    pdf=send(s,user,callback=button(card,'📄 Скачать PDF-отчёт'))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(pdf.generated_document))).pages)
    assert '300,60' in text and 'На что потратили' in text
    expense(s,user,'999')
    again=send(s,user,callback=button(card,'📄 Скачать PDF-отчёт'))
    assert '1 299,60' not in ''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(again.generated_document))).pages)
    assert not ai.calls


def test_empty_report_no_ai(reporting):
    s,ai,_=reporting;user=next(USERS)
    card=send(s,user,'/report')
    assert 'За этот период записей' in card.text
    assert 'не вызывается' in send(s,user,callback=button(card,'🤖 Анализ расходов')).text
    assert not ai.calls


def test_ai_consent_cache_and_retry(reporting):
    s,ai,_=reporting;user=next(USERS);expense(s,user)
    consent=send(s,user,'/analyze')
    assert not ai.calls and 'Описания покупок' in consent.text
    ai.fail=True
    callback=button(consent,'Разрешить и проанализировать')
    failed=send(s,user,callback=callback)
    send(s,user,callback=callback)
    assert len(ai.calls)==1
    ai.fail=False
    done=send(s,user,callback=button(failed,'Повторить'))
    assert 'AI-анализ' in done.text and len(ai.calls)==2
    send(s,user,callback=callback)
    assert len(ai.calls)==2


def test_ai_concurrent(reporting):
    s,ai,_=reporting;user=next(USERS);expense(s,user);entered=Event();release=Event();original=ai.analyze
    def blocked(snapshot):
        entered.set();assert release.wait(5);return original(snapshot)
    ai.analyze=blocked
    consent=send(s,user,'/analyze');callback=button(consent,'Разрешить и проанализировать')
    with ThreadPoolExecutor() as pool:
        future=pool.submit(send,s,user,callback=callback)
        assert entered.wait(5)
        assert 'выполняется' in send(s,user,callback=callback).text
        release.set();future.result()
    assert len(ai.calls)==1


def test_sheets_disabled_in_report_and_old_buttons(reporting):
    s,_,sheets=reporting;user=next(USERS);expense(s,user)
    assert 'отключено' in send(s,user,'/sheets connect https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit').text
    card=send(s,user,'/report')
    assert not any('Google' in label for row in card.buttons for label,_ in row)
    from uuid import uuid4
    for action in ('sverify','rsask','rexport'):
        assert 'отключено' in send(s,user,callback=action+':'+str(uuid4())).text
    assert not sheets.calls


def test_disabled_sheets_does_not_create_connection(reporting,database):
    s,_,sheets=reporting;user=next(USERS)
    send(s,user,'/sheets connect https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit')
    assert query(database,user,'SELECT state FROM sheets_connections')==[]


def test_expired_snapshots_and_bad_callback(reporting,database):
    s,_,_=reporting;user=next(USERS);card=send(s,user,'/report')
    # Owner migration connection still obeys FORCE RLS; use the actor context.
    import psycopg
    with psycopg.connect(database[0]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute("UPDATE balans.reports SET expires_at=now()-interval '1 day'")
    assert 'истёк' in send(s,user,callback=button(card,'📄 Скачать PDF-отчёт')).text
    assert 'bad' not in send(s,user,callback='rpdf:bad').text


def test_csv_formula_injection_and_pdf_markup():
    snapshot=fixture();snapshot['rows'][0]['description']='=HYPERLINK("evil")'
    data=csv_bytes(snapshot).decode('utf-8-sig')
    assert "'=HYPERLINK" in data
    snapshot['rows'][-1]['description']='<b>Покупка & магазин</b>'
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(render_pdf(snapshot))).pages)
    assert '<b>Покупка & магазин</b>' in text


def test_generated_document_transport(reporting):
    import asyncio
    from aiogram.types import Update
    from balans.__main__ import process_update
    from test_receipt_transport import MediaBot
    s,_,_=reporting;user=next(USERS);bot=MediaBot(b'')
    message={'message_id':900001,'date':int(datetime.now(timezone.utc).timestamp()),'text':'/pdf',
             'chat':{'id':user,'type':'private'},'from':{'id':user,'is_bot':False,'first_name':'Test'}}
    update=Update.model_validate({'update_id':900001,'message':message})
    asyncio.run(process_update(bot,s,update))
    assert not bot.messages
    assert len(bot.documents)==1
    assert bot.documents[0].filename.endswith('.pdf')
    assert bot.documents[0].data.startswith(b'%PDF')
    assert 'За этот период записей' in ''.join(page.extract_text() for page in PdfReader(BytesIO(bot.documents[0].data)).pages)


def test_pdf_contains_every_operation_and_income():
    from scripts.check_report_pdf import fixture
    from balans.report_pdf import render_pdf
    from balans.report_data import summarize
    from datetime import date
    s=fixture()
    for i,row in enumerate(s['rows']):row['description']=f'Покупка номер {i:03d}'
    s['rows'] += [dict(s['rows'][0],id='income',kind='income',amount='100000',description='Зарплата за месяц'),dict(s['rows'][0],id='opening',kind='opening',amount='5000',description='Деньги до начала учёта')]
    s['summary']=summarize(s['rows'],date(2026,9,1),date(2026,9,10))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(render_pdf(s))).pages)
    for row in s['rows']:assert row['description'] in text
    assert '100 000,00' in text and 'Начальный остаток' in text


def test_simple_report_has_export_and_refresh_actions(reporting):
    s,_,_=reporting;u=next(USERS)
    card=send(s,u,'/report')
    assert [label for row in card.buttons for label,_ in row]==['📄 Скачать PDF-отчёт','🧾 Детализированный отчёт','📑 CSV за период','🔄 Обновить','🤖 Анализ расходов','📅 Изменить период','☰ Меню']
    assert 'Europe/Moscow' not in card.text and 'Валюта:' not in card.text
