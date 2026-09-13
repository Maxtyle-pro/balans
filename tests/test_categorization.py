from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from balans.ai import CategoryAI, Suggestion
from balans.service import Service

ids=count(100000)
users=count(30000000)
NOW=datetime.now(timezone.utc)


class FakeAI:
    available=True
    model='test-model'

    def __init__(self,category='Кафе и рестораны',error=None):
        self.calls=[]
        self.category=category
        self.error=error

    def classify(self,request):
        self.calls.append(request)
        if self.error:
            return Suggestion(error_code=self.error)
        cat=next(c for c in request['categories'] if c['name']==self.category)
        return Suggestion(cat['id'],0.95,response_id='resp_test',input_tokens=20,output_tokens=10)

    def close(self):
        pass


@pytest.fixture
def ai_service(database):
    ai=FakeAI()
    service=Service(database[1],ai=ai)
    yield service,ai
    service.close()


def send(service,user,text='',callback=None,update=None):
    return service.handle(user,42,next(ids) if update is None else update,text,NOW,callback)


def button(reply,label):
    return next(data for row in reply.buttons for title,data in row if title==label)


def query(database,user,sql,args=()):
    with psycopg.connect(database[1]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        return c.execute(sql,args).fetchall()


def suggest(service,user,description='Кофе в кофейне'):
    send(service,user,'/ai on')
    assert 'На что потратили' in send(service,user,'/add 250').text
    return send(service,user,description)


def save_suggestion(service,user,reply):
    send(service,user,callback=button(reply,'Подтвердить категорию'))
    card=send(service,user,'сегодня')
    return send(service,user,callback=button(card,'Сохранить'))


def test_auto_flow_and_durable_replay(ai_service,database):
    service,ai=ai_service
    user=next(users)
    send(service,user,'/ai on')
    send(service,user,'/add')
    send(service,user,'250')
    update=next(ids)
    reply=send(service,user,'Кофе в кофейне',update=update)
    assert 'AI предлагает: Кафе и рестораны' in reply.text
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    again=Service(database[1],ai=ai)
    try:
        assert send(again,user,'Кофе в кофейне',update=update).text==reply.text
    finally:
        again.close()
    assert len(ai.calls)==1
    assert set(ai.calls[0])=={'description','categories'}
    save_suggestion(service,user,reply)
    assert query(database,user,'SELECT delta FROM postings')==[(Decimal('-250'),)]
    assert query(database,user,'SELECT state,prompt_version,input_tokens FROM ai_jobs')==[('succeeded','category-v1',20)]


def test_no_key_and_automatic_recognition(service,ai_service,database):
    user=next(users)
    send(service,user,'/ai on')
    send(service,user,'/add 250')
    assert 'недоступен' in send(service,user,'Кофе').text
    s,ai=ai_service
    other=next(users)
    send(s,other,'/add 250')
    assert 'AI предлагает' in send(s,other,'Кофе').text
    assert len(ai.calls)==1
    assert query(database,other,'SELECT ai_enabled FROM user_settings')==[(True,)]


def test_correction_requires_opt_in_and_is_private(ai_service,database):
    service,ai=ai_service
    alice,bob=next(users),next(users)
    offered=suggest(service,alice)
    choices=send(service,alice,callback=button(offered,'Другая категория'))
    corrected=send(service,alice,callback=button(choices,'Продукты'))
    learn=button(corrected,'Запомнить для меня')
    assert query(database,alice,'SELECT count(*) FROM category_rules')==[(0,)]
    assert 'недоступно' in send(service,bob,callback=learn).text
    assert 'Запомнил' in send(service,alice,callback=learn).text
    assert 'уже' in send(service,alice,callback=learn).text
    card=send(service,alice,'сегодня')
    send(service,alice,callback=button(card,'Сохранить'))
    repeat=suggest(service,alice,'КОФЕ в кофейне!')
    assert 'Ваше личное правило: Продукты' in repeat.text
    assert len(ai.calls)==1
    assert 'AI предлагает: Кафе и рестораны' in suggest(service,bob).text
    for table in ('category_rules','category_feedback'):
        assert query(database,bob,f'SELECT count(*) FROM {table}')==[(0,)]


def test_saved_category_revision_no_new_money(ai_service,database):
    service,ai=ai_service
    user=next(users)
    saved=save_suggestion(service,user,suggest(service,user))
    choices=send(service,user,callback=button(saved,'Исправить категорию'))
    choice=button(choices,'Продукты')
    corrected=send(service,user,callback=choice)
    assert 'обновлена' in corrected.text
    assert 'устарел' in send(service,user,callback=choice).text
    assert query(database,user,'SELECT revision_no FROM operation_revisions ORDER BY revision_no')==[(1,),(2,)]
    assert query(database,user,'SELECT count(*),sum(delta) FROM postings')==[(1,Decimal('-250'))]
    assert 'Продукты — 250,00' in send(service,user,'/report').text
    assert 'Запомнил' in send(service,user,callback=button(corrected,'Запомнить для меня')).text
    assert query(database,user,"SELECT count(*) FROM audit_log WHERE action='category_changed'")==[(1,)]
    # History can open category correction on any saved operation, including older ones.
    history=send(service,user,'/history')
    send(service,user,callback=button(history,'✏️ Изменить расход'))
    edit=send(service,user,'1')
    choices=send(service,user,callback=button(edit,'Категория'))
    assert button(choices,'Продукты')


def test_old_feedback_and_foreign_saved_actions(ai_service,database):
    service,ai=ai_service
    alice,bob=next(users),next(users)
    saved=save_suggestion(service,alice,suggest(service,alice))
    open_button=button(saved,'Исправить категорию')
    assert 'недоступна' in send(service,bob,callback=open_button).text
    choices=send(service,alice,callback=open_button)
    assert 'устарел' in send(service,bob,callback=button(choices,'Продукты')).text
    changed=send(service,alice,callback=button(choices,'Продукты'))
    choices=send(service,alice,callback=open_button)
    send(service,alice,callback=button(choices,'Дом'))
    assert 'не сохранено' in send(service,alice,callback=button(changed,'Запомнить для меня')).text
    assert query(database,alice,'SELECT count(*) FROM category_rules')==[(0,)]


def test_cancelled_draft_cannot_teach_and_stale_save(ai_service,database):
    service,ai=ai_service
    user=next(users)
    proposed=suggest(service,user)
    send(service,user,callback=button(proposed,'Подтвердить категорию'))
    card=send(service,user,'сегодня')
    changed=send(service,user,'/category Продукты')
    assert 'устарела' in send(service,user,callback=button(card,'Сохранить')).text
    send(service,user,'/cancel')
    assert 'не сохранено' in send(service,user,callback=button(changed,'Запомнить для меня')).text
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]


def test_rules_disable_and_explicit_category_wins(ai_service,database):
    service,ai=ai_service
    user=next(users)
    send(service,user,'/rule кофе | Продукты')
    send(service,user,'/ai on')
    send(service,user,'/add 250')
    send(service,user,'/category Дом')
    assert 'Когда потратили' in send(service,user,'Кофе').text
    assert not ai.calls
    send(service,user,'/cancel')
    proposed=suggest(service,user,'Кофе')
    assert 'Ваше личное правило: Продукты' in proposed.text
    send(service,user,'/cancel')
    rules=send(service,user,'/rules')
    send(service,user,callback=button(rules,'Отключить №1'))
    assert 'AI предлагает' in suggest(service,user,'Кофе').text
    assert len(ai.calls)==1


def test_failed_ai_is_cached_and_manual_still_saves(ai_service,database):
    service,ai=ai_service
    ai.error='APITimeoutError'
    user=next(users)
    send(service,user,'/ai on');send(service,user,'/add 250')
    update=next(ids)
    reply=send(service,user,'Кофе',update=update)
    assert 'вручную' in reply.text
    send(service,user,'Кофе',update=update)
    assert len(ai.calls)==1
    send(service,user,callback=button(reply,'Кафе и рестораны'))
    card=send(service,user,'сегодня')
    send(service,user,callback=button(card,'Сохранить'))
    assert query(database,user,'SELECT state,error_code FROM ai_jobs')==[('failed','APITimeoutError')]
    assert query(database,user,'SELECT count(*) FROM operations')==[(1,)]


@pytest.mark.parametrize('during',['/cancel','/category Дом'])
def test_late_result_never_overwrites_user_action(ai_service,database,during):
    service,ai=ai_service
    user=next(users)
    entered=Event();release=Event()
    classify=ai.classify
    def delayed(request):
        entered.set()
        assert release.wait(5)
        return classify(request)
    ai.classify=delayed
    send(service,user,'/ai on');send(service,user,'/add 250')
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(send,service,user,'Кофе')
        assert entered.wait(5)
        try:
            send(service,user,during)
        finally:
            release.set()
        assert 'отменён' in future.result().text
    assert query(database,user,'SELECT state FROM ai_jobs')==[('cancelled',)]
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    if during=='/category Дом':
        assert query(database,user,'SELECT c.name FROM operation_drafts d JOIN categories c ON c.id=d.category_id')==[('Дом',)]


def test_foreign_category_and_rule_rls(ai_service,database):
    service,_=ai_service
    alice,bob=next(users),next(users)
    send(service,alice,'/rule кофе | Дом')
    send(service,bob,'/start')
    cat=query(database,bob,'SELECT id FROM categories LIMIT 1')[0][0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        query(database,alice,'UPDATE category_rules SET category_id=%s RETURNING id',(cat,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        query(database,alice,'UPDATE operation_revisions SET category_id=%s',(cat,))
