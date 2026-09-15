from itertools import count
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_service import send,query,draft
from test_receipts import button

USERS=count(80000000)


def confirm(s,u,reply):return send(s,u,callback=next(data for row in reply.buttons for label,data in row if label in ('Подтвердить','Сохранить изменения')))
def operation(db,u,kind):return str(query(db,u,'SELECT id FROM operations WHERE kind=%s ORDER BY created_at DESC LIMIT 1',(kind,))[0][0])
def balances(db,u):return dict(query(db,u,'SELECT a.name,coalesce(sum(p.delta),0) FROM accounts a LEFT JOIN postings p ON p.account_id=a.id GROUP BY a.id,a.name'))


def test_income_opening_transfer_and_reports(service,database):
    s=service;u=next(USERS)
    confirm(s,u,send(s,u,'/opening 1000 | сегодня'))
    confirm(s,u,send(s,u,'/income 500 | Зарплата | сегодня'))
    send(s,u,'/account Наличные')
    confirm(s,u,send(s,u,'/transfer 200 | Наличные | сегодня'))
    assert balances(database,u)=={'Основной':Decimal('1300'),'Наличные':Decimal('200')}
    assert '0,00' in send(s,u,'/report').text
    with s._actor_transaction(u) as c:
        report=s._report_snapshot(c,c.execute('SELECT actor_user_id() AS id').fetchone()['id'],'',__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
        assert Decimal(report['snapshot']['summary']['income'])==500
        assert Decimal(report['snapshot']['summary']['total'])==0
        assert len(report['snapshot']['rows'])==3
    assert query(database,u,"SELECT sum(p.delta) FROM postings p JOIN journal_entries e ON e.id=p.entry_id WHERE e.event_kind='transfer'")==[(Decimal(0),)]


def test_account_selection_freezes_draft(service,database):
    s=service;u=next(USERS);card=send(s,u,'/account Карта')
    query(database,u,"UPDATE user_settings SET default_account_id=(SELECT id FROM accounts WHERE name='Карта') RETURNING user_id")
    save=draft(s,u,'100')
    query(database,u,"UPDATE user_settings SET default_account_id=(SELECT id FROM accounts WHERE name='Основной') RETURNING user_id")
    send(s,u,callback=save)
    assert balances(database,u)=={'Основной':Decimal(0),'Карта':Decimal(-100)}


def test_refund_partial_limits_and_cancel(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    original=operation(database,u,'expense')
    card=send(s,u,callback='frefund:'+original);card=send(s,u,'40')
    confirm(s,u,card)
    refund=operation(database,u,'refund')
    send(s,u,callback='frefund:'+original);card=send(s,u,'70')
    assert 'превышают' in confirm(s,u,card).text
    assert balances(database,u)['Основной']==Decimal(-60)
    send(s,u,'/cancel')
    card=send(s,u,callback='fdelete:'+original);card=send(s,u,'Ошибка')
    assert 'возвраты' in confirm(s,u,card).text
    send(s,u,'/cancel')
    send(s,u,callback='fdelete:'+refund);card=send(s,u,'Ошибочный возврат');confirm(s,u,card)
    send(s,u,callback='fdelete:'+original);card=send(s,u,'Ошибочная покупка');confirm(s,u,card)
    assert balances(database,u)['Основной']==0
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='cancelled'")==[(2,)]


def test_revision_and_stale_callbacks(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    identity=operation(database,u,'expense');card=send(s,u,callback='fedit:'+identity)
    assert 'Сохранить изменения' not in [label for row in card.buttons for label,_ in row]
    stale=button(card,'Отмена').replace('cancel:','fsave:')
    send(s,u,callback=button(card,'Изменить сумму'));card=send(s,u,'150')
    assert '150,00' in card.text
    assert [label for row in card.buttons for label,_ in row]==['✏️ Изменить']
    stale_reply=send(s,u,callback=stale)
    assert 'устарела' in stale_reply.text
    assert not any(label=='Продолжить' for row in stale_reply.buttons for label,_ in row)
    assert button(stale_reply,'Открыть историю')=='history'
    assert balances(database,u)['Основной']==-150
    assert query(database,u,'SELECT count(*) FROM operation_revisions')==[(2,)]
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert '150,00' in send(s,u,'/report').text
    card=send(s,u,callback='fedit:'+identity);send(s,u,callback=button(card,'Изменить дату'));card=send(s,u,'вчера')
    assert balances(database,u)['Основной']==-150
    assert query(database,u,'SELECT count(*) FROM operation_revisions')==[(3,)]


def test_transfer_edit_reverses_both_sides(service,database):
    s=service;u=next(USERS);send(s,u,'/account Наличные')
    confirm(s,u,send(s,u,'/transfer 500 | Наличные'))
    card=send(s,u,callback='fedit:'+operation(database,u,'transfer'))
    send(s,u,callback=button(card,'Изменить сумму'));card=send(s,u,'200')
    assert balances(database,u)=={'Основной':Decimal(-200),'Наличные':Decimal(200)}


def test_private_operations_and_opening_once(service,database):
    s=service;u=next(USERS);other=next(USERS)
    confirm(s,u,send(s,u,'/opening -100'))
    identity=operation(database,u,'opening')
    assert 'недоступна' in send(s,other,callback='fedit:'+identity).text
    reopened=send(s,u,'/opening 500')
    assert 'уже задан' in reopened.text
    assert 'Сохранить изменения' not in [label for row in reopened.buttons for label,_ in row]
    assert balances(database,u)['Основной']==-100
    assert query(database,other,'SELECT * FROM postings')==[]


def test_changed_category_keeps_financial_correction_valid(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    identity=operation(database,u,'expense')
    menu=send(s,u,callback='opcat:'+identity)
    send(s,u,callback=button(menu,'Дом'))
    card=send(s,u,callback='fedit:'+identity)
    send(s,u,callback=button(card,'Изменить сумму'));card=send(s,u,'70')
    assert balances(database,u)['Основной']==-70
    assert query(database,u,'SELECT count(*) FROM operation_revisions')==[(3,)]
