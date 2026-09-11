from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count

import psycopg
import pytest

from balans.migrate import migrate
from balans.service import Service

ids = count(1000)
users = count(10000000)
NOW = datetime.now(timezone.utc)


def send(service, user, text='', callback=None, update=None):
    return service.handle(user, 42, next(ids) if update is None else update, text, NOW, callback)


def draft(service,user,amount='850,50'):
    send(service,user,'/manual ' + amount)
    send(service,user,'Продукты')
    send(service,user,'Тестовый расход')
    reply = send(service,user,'сегодня')
    assert 'Пока не сохранён' not in reply.text
    assert 'Сумма:' in reply.text
    return reply.buttons[0][0][1]


def query(database,user,statement,args=()):
    with psycopg.connect(database[1]) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(user),))
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        return c.execute(statement,args).fetchall()


def test_registration_and_expense(service,database):
    user=next(users)
    send(service,user,'/start')
    send(service,user,'/start')
    assert query(database,user,'SELECT count(*) FROM users')[0][0]==1
    assert query(database,user,'SELECT count(*) FROM workspaces')[0][0]==1
    button=draft(service,user)
    assert '850,50' not in send(service,user,'/history').text
    assert 'сохранён' in send(service,user,callback=button).text
    assert '850,50' in send(service,user,'/history').text
    assert '850,50' in send(service,user,'/report').text
    assert '-850,50' in send(service,user,'/accounts').text
    assert query(database,user,'SELECT delta FROM postings')==[(Decimal('-850.50'),)]
    assert query(database,user,'SELECT count(*) FROM audit_log')==[(1,)]


def test_concurrent_save_and_update_replay(service,database):
    user=next(users)
    button=draft(service,user)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies=list(pool.map(lambda _:send(service,user,callback=button),range(4)))
    assert sum('уже сохранён' in r.text for r in replies)==3
    assert query(database,user,'SELECT count(*) FROM operations')==[(1,)]
    update=next(ids)
    first=send(service,user,'/manual 10',update=update)
    assert send(service,user,'/manual 10',update=update).text==first.text
    assert query(database,user,"SELECT step FROM operation_drafts WHERE state='pending'")==[('category',)]


def test_restart_cancel_and_expiry(service,database):
    user=next(users)
    send(service,user,'/manual 20')
    other=Service(database[1])
    try:
        assert 'На что потратили' in send(other,user,'Другое').text
    finally:
        other.close()
    send(service,user,'/cancel')
    button=draft(service,user)
    query(database,user,"UPDATE operation_drafts SET expires_at=now()-interval '1 second' WHERE state='pending' RETURNING id")
    assert 'истёк' in send(service,user,callback=button).text
    assert query(database,user,'SELECT count(*) FROM postings')==[(0,)]


def test_rls_and_forged_callbacks(service,database):
    alice,bob=next(users),next(users)
    button=draft(service,alice)
    send(service,bob,'/start')
    assert 'недоступен' in send(service,bob,callback=button).text
    assert query(database,bob,'SELECT count(*) FROM operation_drafts')==[(0,)]
    send(service,alice,callback=button)
    assert query(database,bob,'SELECT count(*) FROM operations')==[(0,)]
    assert query(database,bob,'SELECT count(*) FROM postings')==[(0,)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        query(database,alice,'UPDATE postings SET delta=-1')
    category=query(database,alice,'SELECT id FROM categories LIMIT 1')[0][0]
    draft(service,bob)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        query(database,bob,'UPDATE operation_drafts SET category_id=%s RETURNING id',(category,))


def test_validation_navigation_settings(service,database):
    user=next(users)
    send(service,user,'/manual')
    assert 'положительную' in send(service,user,'-20').text
    send(service,user,'100')
    assert 'не найдена' in send(service,user,'Не категория').text
    send(service,user,'Дом')
    assert '500' in send(service,user,'x'*501).text
    send(service,user,'-')
    assert 'дату' in send(service,user,'не дата').text
    send(service,user,'/cancel')
    assert 'Неизвестный часовой' in send(service,user,'/settings No/Zone').text
    assert 'Asia/Tokyo' in send(service,user,'/settings Asia/Tokyo').text
    assert 'положительным целым' in send(service,user,'/history -1').text
    for _ in range(6):
        send(service,user,callback=draft(service,user,'1'))
    assert '/history 2' in send(service,user,'/history').text
    assert '/history 1' in send(service,user,'/history 2').text


def test_migration_replay(database):
    migrate(database[0],database[2])


def test_edit_invalidates_old_buttons(service,database):
    user=next(users)
    old=draft(service,user)
    send(service,user,callback=old.replace('save:', 'edit:'))
    send(service,user,'12')
    send(service,user,'Другое')
    send(service,user,'-')
    reply=send(service,user,'сегодня')
    assert 'недоступен' in send(service,user,callback=old).text
    send(service,user,callback=reply.buttons[0][0][1])
    assert query(database,user,'SELECT delta FROM postings')==[(Decimal('-12'),)]


def test_incomplete_draft_never_posts(service,database):
    user=next(users)
    send(service,user,'/manual')
    draft_id=query(database,user,'SELECT id FROM operation_drafts')[0][0]
    with pytest.raises(psycopg.errors.RaiseException):
        query(database,user,'SELECT save_expense(%s)',(draft_id,))
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    assert query(database,user,'SELECT count(*) FROM postings')==[(0,)]
