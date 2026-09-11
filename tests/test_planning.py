from datetime import datetime,timezone,timedelta,date
from itertools import count
from balans.planning import budget_period,next_allowed,latest_slot
from test_service import send,query,draft,NOW
from test_funds import setup,issue
from test_receipts import button

USERS=count(140000000)

def ready(s,user):
    send(s,user,'/notify on');send(s,user,'/notify quiet off')

def test_period_and_quiet_dst():
    assert budget_period(date(2026,1,3),5)==(date(2025,12,5),date(2026,1,4))
    assert budget_period(date(2024,2,29),28)==(date(2024,2,28),date(2024,3,27))
    now=datetime(2026,9,10,20,tzinfo=timezone.utc)
    assert next_allowed(now,1320,540,'Europe/Moscow')==datetime(2026,9,11,6,tzinfo=timezone.utc)
    assert next_allowed(now,None,None,'Europe/Moscow')==now
    fall=datetime(2026,10,25,0,30,tzinfo=timezone.utc)
    assert next_allowed(fall,120,180,'Europe/Berlin')==datetime(2026,10,25,2,tzinfo=timezone.utc)
    assert latest_slot(now,'Europe/Moscow',1140,0).weekday()==0


def test_budget_threshold_once_and_optin(service,database):
    s=service;u=next(USERS)
    send(s,u,'/budget 1000');send(s,u,callback=draft(s,u,'850'))
    s.plan_notifications(NOW+timedelta(seconds=1))
    assert not query(database,u,'SELECT * FROM budget_alerts')
    ready(s,u);s.plan_notifications(NOW+timedelta(minutes=60))
    assert query(database,u,'SELECT threshold FROM budget_alerts')==[(80,)]
    send(s,u,callback=draft(s,u,'200'))
    s.plan_notifications(NOW+timedelta(minutes=62))
    assert sorted(query(database,u,'SELECT threshold FROM budget_alerts'))==[(80,),(100,)]
    send(s,u,'/budget 900');s.plan_notifications(NOW+timedelta(minutes=64))
    assert query(database,u,'SELECT count(*) FROM notification_outbox')==[(2,)]
    assert '1 050,00' in send(s,u,'/budget').text
    assert 'Продукты' in send(s,u,'/budget 1500 | Продукты').text
    assert 'число' in send(s,u,'/budgetday 31').text
    send(s,u,'/notify off')
    assert query(database,u,"SELECT count(*) FROM notification_outbox WHERE state='pending'")==[(0,)]


def test_notifications_quiet_block_and_isolation(service,database):
    s=service;u=next(USERS);v=next(USERS)
    ready(s,u);send(s,u,'/budget 100');send(s,u,callback=draft(s,u,'150'))
    moment=NOW+timedelta(minutes=60);s.plan_notifications(moment)
    own=[j for j in s.claim_notifications(moment) if j['telegram_user_id']==u]
    assert len(own)==1
    identity=own[0]['id']
    send(s,v,'/start')
    assert not query(database,v,'SELECT id FROM notification_outbox WHERE id=%s',(identity,))
    assert s.prepare_notification(v,identity,moment) is None
    assert s.prepare_notification(u,identity,moment)['telegram_user_id']==u
    s.notifications_blocked(u)
    assert s.prepare_notification(u,identity,moment) is None
    assert query(database,u,'SELECT blocked FROM notification_preferences')==[(True,)]
    send(s,u,'/start')
    assert query(database,u,'SELECT blocked FROM notification_preferences')==[(False,)]


def test_shared_event_optin_and_revoked_access(service,database):
    s=service;owner,user=setup(s)
    ready(s,owner);send(s,owner,'/notify events instant')
    send(s,user,callback=draft(s,user,'80'))
    assert query(database,owner,'SELECT count(*) FROM notification_events')==[(1,)]
    moment=NOW+timedelta(minutes=60);s.plan_notifications(moment)
    job=[j for j in s.claim_notifications(moment) if j['telegram_user_id']==owner][0]
    n=s.prepare_notification(owner,job['id'],moment)
    assert n and n['entity_kind']=='operation'
    assert 'Документы' in send(s,owner,callback=f"nopen:{job['id']}").text
    ready(s,user);send(s,user,'/notify events instant');issue(s,owner,user,'100')
    s.plan_notifications(NOW+timedelta(minutes=62))
    jobs=[j for j in s.claim_notifications(NOW+timedelta(minutes=62)) if j['telegram_user_id']==user]
    assert jobs
    r=send(s,owner,'/members');r=send(s,owner,callback=button(r,f'Исключить {user}'));send(s,owner,callback=button(r,'Подтвердить прекращение доступа'))
    assert s.prepare_notification(user,jobs[0]['id'],NOW+timedelta(minutes=62)) is None
    assert 'прекращён' in send(s,user,callback=f"nopen:{jobs[0]['id']}").text


def test_weekly_and_reminder_dedupe(service,database):
    s=service;u=next(USERS);ready(s,u)
    send(s,u,'/notify weekly on');send(s,u,'/notify reminder on')
    future=NOW+timedelta(days=8)
    s.plan_notifications(future);s.plan_notifications(future+timedelta(minutes=2))
    rows=query(database,u,'SELECT kind,entity_kind FROM notification_outbox ORDER BY kind')
    assert rows==[('reminder','budget'),('weekly','report')]
    identity=query(database,u,"SELECT id FROM notification_outbox WHERE kind='weekly'")[0][0]
    assert 'Расходы:' in send(s,u,callback=f'nopen:{identity}').text


def test_daily_digest_and_quiet_delivery(service,database):
    s=service;owner,user=setup(s);ready(s,owner);send(s,owner,'/notify events daily')
    send(s,user,callback=draft(s,user,'50'));send(s,user,callback=draft(s,user,'60'))
    future=NOW+timedelta(days=2);s.plan_notifications(future);s.plan_notifications(future+timedelta(minutes=2))
    assert query(database,owner,"SELECT count(*) FROM notification_outbox WHERE kind='digest'")==[(1,)]
    assert '2' in query(database,owner,"SELECT message FROM notification_outbox WHERE kind='digest'")[0][0]
    job=[j for j in s.claim_notifications(future) if j['telegram_user_id']==owner][0]
    local=future.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow'))
    a=local.hour*60+local.minute;b=(a+60)%1440
    send(s,owner,f'/notify quiet {a//60:02}:{a%60:02} {b//60:02}:{b%60:02}')
    assert s.prepare_notification(owner,job['id'],future) is None
    assert query(database,owner,'SELECT state FROM notification_outbox WHERE id=%s',(job['id'],))==[('pending',)]
    later=future+timedelta(hours=2)
    jobs=[j for j in s.claim_notifications(later) if j['id']==job['id']]
    assert jobs and s.prepare_notification(owner,job['id'],later)
    s.claim_notifications(later+timedelta(minutes=3))
    assert query(database,owner,'SELECT state FROM notification_outbox WHERE id=%s',(job['id'],))==[('uncertain',)]
