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
    assert 'не вычисляется' in card.text
    assert query(database,other,'SELECT * FROM reports')==[]
    assert 'недоступен' in send(s,other,callback=button(card,'PDF с графиками')).text
    pdf=send(s,user,callback=button(card,'PDF с графиками'))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(pdf.generated_document))).pages)
    assert '300,60' in text and 'Структура расходов' in text
    expense(s,user,'999')
    again=send(s,user,callback=button(card,'PDF с графиками'))
    assert '1 299,60' not in ''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(again.generated_document))).pages)
    assert not ai.calls


def test_empty_report_no_ai(reporting):
    s,ai,_=reporting;user=next(USERS)
    card=send(s,user,'/report')
    assert 'Нет операций' in card.text
    assert 'не вызывается' in send(s,user,callback=button(card,'AI-анализ')).text
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


def test_sheets_verified_explicit_export_and_revoke(reporting):
    s,_,sheets=reporting;user=next(USERS);expense(s,user)
    assert 'подключены' in connect(s,user).text
    card=send(s,user,'/report')
    consent=send(s,user,callback=button(card,'Google Sheets'))
    assert 'описания' in consent.text and not sheets.calls
    callback=button(consent,'Экспортировать в эту таблицу')
    done=send(s,user,callback=callback);send(s,user,callback=callback)
    assert 'Экспорт готов' in done.text and len(sheets.calls)==1
    assert sheets.calls[0][-1]['summary']['total']=='100.250000'
    card=send(s,user,'/report');consent=send(s,user,callback=button(card,'Google Sheets'))
    send(s,user,'/sheets off')
    assert 'изменилось' in send(s,user,callback=button(consent,'Экспортировать в эту таблицу')).text
    assert len(sheets.calls)==1


def test_wrong_challenge_not_connected(reporting,database):
    s,_,sheets=reporting;user=next(USERS);sheets.valid=False
    assert 'не подтверждён' in connect(s,user).text
    assert query(database,user,"SELECT state FROM sheets_connections")==[('pending',)]


def test_expired_snapshots_and_bad_callback(reporting,database):
    s,_,_=reporting;user=next(USERS);card=send(s,user,'/report')
    # Owner migration connection still obeys FORCE RLS; use the actor context.
    import psycopg
    with psycopg.connect(database[0]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute("UPDATE balans.reports SET expires_at=now()-interval '1 day'")
    assert 'истёк' in send(s,user,callback=button(card,'PDF с графиками')).text
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
    assert bot.documents[0].filename.endswith('.pdf')
    assert bot.documents[0].data.startswith(b'%PDF')
    assert 'Нет операций' in ''.join(page.extract_text() for page in PdfReader(BytesIO(bot.documents[0].data)).pages)
