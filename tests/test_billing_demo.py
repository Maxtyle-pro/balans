import asyncio
from concurrent.futures import ThreadPoolExecutor
from itertools import count

import psycopg
import pytest
from balans.__main__ import deliver_reply
from scripts.configure_billing_demo import configure
from test_service import send,query,draft
from test_receipts import button
from test_billing import billing,invoice,payment

USERS=count(230000000)


def enable(s,db):
    u=next(USERS);send(s,u,'/start');configure(db[0],u)
    return u


def checkout(s,u):
    r=send(s,u,callback='billdetails')
    return send(s,u,callback=button(r,'Прочитал условия, перейти к оплате'))


def test_demo_full_flow_without_real_payment(service,database):
    s=service;u=enable(s,database)
    r=send(s,u,'/start');assert 'Тестовая оплата' in r.text
    assert len(r.buttons)==2
    r=send(s,u,callback=button(r,'Начать 7 дней бесплатно'))
    assert '100 Stars' in r.text
    send(s,u,callback=button(r,'Понятно, начать'))
    assert query(database,u,'SELECT trial_started_at FROM billing_accounts')==[(None,)]
    send(s,u,callback=draft(s,u,'10'))
    r=send(s,u,'/demo');send(s,u,callback=button(r,'Завершить период'))
    assert 'Доступ истёк' in send(s,u,'/subscription').text
    r=checkout(s,u);assert not r.invoice_id and 'Stars не списываются' in r.text
    pay=button(r,'Оплатить тестово')
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:send(s,u,callback=pay),range(2)))
    assert sum('успешно имитирована' in r.text for r in results)==1
    deadline=query(database,u,'SELECT paid_until FROM billing_demo')[0][0]
    send(s,u,callback=pay)
    assert query(database,u,'SELECT paid_until FROM billing_demo')==[(deadline,)]
    assert 'Оплачено' in send(s,u,'/subscription').text
    assert not query(database,u,'SELECT id FROM billing_invoices')
    assert not query(database,u,'SELECT charge_id FROM billing_payments')
    assert not query(database,u,'SELECT id FROM billing_audit')
    renew=send(s,u,'/renewal')
    r=send(s,u,callback=button(renew,'Отключить автопродление'))
    assert not r.renewal_invoice_id and 'выключено' in r.text
    assert query(database,u,'SELECT paid_until FROM billing_demo')==[(deadline,)]
    panel=send(s,u,'/demo');r=send(s,u,callback=button(panel,'Новый пользователь'))
    assert 'Начать 7 дней бесплатно' in [label for row in r.buttons for label,_ in row]
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert 'устарел' in send(s,u,callback=pay).text
    send(s,u,'/demo off')
    assert 'Тестовая' not in send(s,u,'/start').text
    assert 'пока не включена' in send(s,u,'/subscription').text


def test_cancel_and_isolation(service,database):
    s=service;u=enable(s,database);v=next(USERS)
    assert 'не включена' in send(s,v,'/demo on').text
    r=checkout(s,u);pay=button(r,'Оплатить тестово')
    assert 'недоступна' in send(s,v,callback=pay).text
    send(s,u,callback=button(r,'Отменить'))
    send(s,u,callback=pay)
    assert query(database,u,'SELECT paid_until FROM billing_demo')==[(None,)]
    assert not query(database,v,'SELECT user_id FROM billing_demo')
    with s._actor_transaction(v) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():
            c.execute('INSERT INTO billing_demo(user_id,allowed) VALUES(actor_user_id(),true)')
    configure(database[0],u,disable=True)
    assert 'не включена' in send(s,u,'/demo on').text
    assert 'выключена' in send(s,u,callback=pay).text


def test_no_telegram_payment_api_called(service,database,billing):
    s=service;u=next(USERS)
    real_invoice=invoice(s,u)
    configure(database[0],u)
    assert s.prepare_invoice(u,real_invoice) is None
    assert not s.pre_checkout(u,'q',real_invoice,'XTR',100)
    assert not send(s,u,callback='billbuy:1').invoice_id
    send(s,u,'/workspace Проверка')
    assert s.prepare_invoice(u,real_invoice) is None
    assert not s.pre_checkout(u,'shared',real_invoice,'XTR',100)
    r=send(s,u,'/workspaces')
    personal=next(data for row in r.buttons for label,data in row if 'Личный' in label)
    send(s,u,callback=personal)
    r=checkout(s,u)
    class Bot:
        def __init__(self):self.messages=[]
        async def send_message(self,chat,text,**kw):self.messages.append(text)
        async def create_invoice_link(self,**kw):raise AssertionError('Real invoice must not be created')
        async def edit_user_star_subscription(self,*args,**kw):raise AssertionError('Real renewal must not be called')
    bot=Bot()
    asyncio.run(deliver_reply(bot,u,r,s))
    r=send(s,u,callback=button(r,'Оплатить тестово'))
    asyncio.run(deliver_reply(bot,u,r,s))
    assert len(bot.messages)==2
    # Old genuine renewal buttons are blocked in simulation, even after a real payment arrives.
    s.record_payment(u,payment(real_invoice,charge=f'demo-real-{u}'))
    assert s.renewal_details(u,real_invoice) is None
    r=send(s,u,callback=f'billrenew:{real_invoice}:0')
    assert not r.renewal_invoice_id
    send(s,u,'/demo off')
    assert s.renewal_details(u,real_invoice)


def test_old_demo_buttons_never_create_real_invoices(service,database,billing):
    s=service;u=enable(s,database)
    details=send(s,u,callback='billdetails');buy=button(details,'Прочитал условия, перейти к оплате')
    offer=send(s,u,callback='trialinfo');accept=button(offer,'Понятно, начать')
    send(s,u,'/demo off')
    assert not send(s,u,callback=buy).invoice_id
    assert 'Условия изменились' in send(s,u,callback=accept).text
    assert query(database,u,'SELECT trial_started_at FROM billing_accounts')==[(None,)]
    assert not query(database,u,'SELECT id FROM billing_invoices')
