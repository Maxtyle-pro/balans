from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import count
from threading import Event
from io import BytesIO
import math
import struct
import wave

import pytest
from balans.service import Service
from balans.voice_ai import VoiceResult, validate_result
from balans.voice_media import prepare_voice
from balans.receipt_media import MediaError
from test_receipts import send, button, query

USERS=count(60000000)
UPDATES=count(600000)
NOW=datetime(2026,9,10,22,tzinfo=timezone.utc)


def audio(seconds=.3, silent=False):
    stream=BytesIO()
    with wave.open(stream,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
        f.writeframes(b''.join(struct.pack('<h',0 if silent else int(1000*math.sin(i*.1))) for i in range(int(seconds*16000))))
    return stream.getvalue()


class FakeVoice:
    available=True;model='test';transcribe_model='test'
    def __init__(self):
        self.transcriptions=0;self.parses=0;self.changes={};self.fail=False
    def transcribe(self,data):
        self.transcriptions+=1
        if self.fail: raise TimeoutError()
        return 'Вчера потратил 850 рублей на продукты'
    def extract(self,transcript,categories,today):
        self.parses+=1
        payload=dict(kind='expense',amount='850',amount_confidence=.99,currency='RUB',description='Продукты',occurred_on='2026-09-10',date_note='Вчера',category_id=categories[0]['id'],category_confidence=.99)
        payload.update(self.changes)
        return VoiceResult(**payload)


@pytest.fixture
def voices(database):
    ai=FakeVoice();s=Service(database[1],voice_ai=ai)
    yield s,ai
    s.close()


def upload(s,user,update=None):
    send(s,user,callback='voice_on')
    return s.receive_voice(user,42,next(UPDATES) if update is None else update,NOW,audio())


def test_audio_validation():
    assert prepare_voice(audio()).startswith(b'RIFF')
    for data in (b'', b'bad', audio(silent=True), audio(180.01)):
        with pytest.raises(MediaError): prepare_voice(data)


def test_voice_confirm_idempotence_and_source(voices,database):
    s,ai=voices;user=next(USERS);update=next(UPDATES)
    card=upload(s,user,update)
    assert 'Описание:' in card.text and '850,00' in card.text
    assert query(database,user,'SELECT * FROM postings')==[]
    assert s.receive_voice(user,42,update,NOW,b'bad').text==card.text
    assert (ai.transcriptions,ai.parses)==(1,1)
    saved=send(s,user,callback=button(card,'Сохранить'))
    assert 'сохранён' in saved.text
    send(s,user,callback=button(card,'Сохранить'))
    assert len(query(database,user,'SELECT * FROM postings'))==1
    assert query(database,user,'SELECT source_kind FROM operation_revisions')==[('voice',)]


def test_missing_fields_edit_category_and_stale_save(voices,database):
    s,ai=voices;user=next(USERS)
    ai.changes={'amount':None,'occurred_on':None,'category_id':None}
    card=upload(s,user)
    assert all(label!='Сохранить' for row in card.buttons for label,_ in row)
    for label,value in [('Сумма','120,50'),('Дата','вчера'),('Описание','Такси')]:
        send(s,user,callback=button(card,label));card=send(s,user,value)
    menu=send(s,user,callback=button(card,'Категория'))
    card=send(s,user,callback=menu.buttons[0][0][1])
    old=button(card,'Сохранить')
    send(s,user,callback=button(card,'Сумма'));card=send(s,user,'121')
    assert 'устарела' in send(s,user,callback=old).text
    send(s,user,callback=button(card,'Сохранить'))
    row=query(database,user,'SELECT amount,description,occurred_on FROM operation_revisions')[0]
    assert str(row[0])=='121.000000' and row[1]=='Такси' and row[2].isoformat()=='2026-09-10'


@pytest.mark.parametrize('changes',[{'kind':'other'},{'kind':'multiple'},{'currency':'USD'}])
def test_non_expense_rejected(voices,database,changes):
    s,ai=voices;user=next(USERS);ai.changes=changes
    assert 'Ничего не сохранено' in upload(s,user).text
    assert query(database,user,'SELECT * FROM operation_drafts')==[]


def test_error_no_automatic_retry(voices,database):
    s,ai=voices;user=next(USERS);ai.fail=True;update=next(UPDATES)
    card=upload(s,user,update)
    assert 'Не удалось' in card.text
    assert s.voice_preflight(user,42,update).text==card.text
    assert ai.transcriptions==1
    assert query(database,user,'SELECT * FROM postings')==[]


def test_isolation_and_duplicate(voices,database):
    s,ai=voices;user=next(USERS);other=next(USERS)
    card=upload(s,user)
    assert 'недоступен' in send(s,other,callback=button(card,'Сохранить')).text.lower()
    assert query(database,other,'SELECT * FROM voice_jobs')==[]
    send(s,user,callback=button(card,'Сохранить'))
    card=upload(s,user)
    assert 'Похожий расход' in card.text
    card=send(s,user,callback=button(card,'Это другая покупка'))
    send(s,user,callback=button(card,'Сохранить'))
    assert len(query(database,user,'SELECT * FROM postings'))==2


@pytest.mark.parametrize('cancel',['/cancel'])
def test_late_result_and_busy(voices,database,cancel):
    s,ai=voices;user=next(USERS);entered=Event();release=Event()
    original=ai.extract
    def blocked(*args):
        entered.set();assert release.wait(5)
        return original(*args)
    ai.extract=blocked
    with ThreadPoolExecutor() as pool:
        future=pool.submit(upload,s,user)
        assert entered.wait(5)
        assert 'обрабатывается' in send(s,user,'/add').text
        send(s,user,cancel);release.set()
        assert 'отменена' in future.result().text
    assert query(database,user,'SELECT * FROM operation_drafts')==[]


def test_interrupted_replay(voices,database):
    s,ai=voices;user=next(USERS);card=upload(s,user)
    with s._actor_transaction(user) as c:
        job=c.execute('SELECT id FROM voice_jobs').fetchone()['id']
        c.execute("UPDATE voice_jobs SET state='processing',reply=NULL,lease_until=now()-interval '1 minute' WHERE id=%s",(job,))
        c.execute("UPDATE operation_drafts SET state='cancelled',voice_job_id=NULL")
    assert 'прервалась' in send(s,user,'/voice').text
    assert ai.transcriptions==1


def test_low_confidence_invalid_date():
    value=VoiceResult(kind='expense',amount='850',amount_confidence=.1,currency='RUB',description='Карта 1234567812345678',occurred_on='2027-01-01',date_note='',category_id='foreign',category_confidence=.99)
    result=validate_result(value,[],'2026-09-10')
    assert result.amount is None and result.occurred_on is None and result.category_id is None
    assert '1234567812345678' not in result.description
