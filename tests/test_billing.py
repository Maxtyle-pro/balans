from datetime import datetime,timezone,timedelta
from itertools import count
from concurrent.futures import ThreadPoolExecutor
import psycopg
import pytest
from balans.service import Service
from scripts.configure_billing import configuration
from test_service import send,query,draft
from test_receipts import button
from test_funds import setup
from test_categorization import FakeAI

USERS=count(150000000)

@pytest.fixture
def billing(database):
    with psycopg.connect(database[0]) as c:
        c.execute("UPDATE balans.billing_config SET enabled=true,stars=100,owner_telegram_id=999,trial_days=7,text_quota=1,voice_seconds=60,image_quota=2,terms_url='https://example.org/terms',support_contact='@support'")
    yield
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET enabled=false')


def invoice(s,u):
    r=send(s,u,'/subscription');return send(s,u,callback=button(r,'Прочитал условия, перейти к оплате')).invoice_id

def payment(identity,charge='charge-1',end=None):
    return {'invoice_payload':identity,'telegram_payment_charge_id':charge,'currency':'XTR','total_amount':100,'is_recurring':True,'is_first_recurring':True,'subscription_expiration_date':int((end or datetime.now(timezone.utc)+timedelta(days=30)).timestamp())}


def test_configuration_requires_explicit_values():
    assert configuration({})=={'enabled':False}
    with pytest.raises(ValueError):configuration({'BILLING_ENABLED':'true'})


def test_checkout_payment_dedup_and_order(service,database,billing):
    s=service;u=next(USERS);v=next(USERS);identity=invoice(s,u)
    send(s,v,'/start')
    assert not s.pre_checkout(v,'q',identity,'XTR',100)
    assert not s.pre_checkout(u,'q',identity,'RUB',100)
    assert not s.pre_checkout(u,'q',identity,'XTR',101)
    assert s.pre_checkout(u,'q',identity,'XTR',100)
    assert s.pre_checkout(u,'q',identity,'XTR',100)
    assert not s.pre_checkout(u,'another',identity,'XTR',100)
    p=payment(identity,charge=f'charge-{u}');s.record_payment(u,p);s.record_payment(u,p)
    assert query(database,u,'SELECT count(*) FROM billing_payments')==[(1,)]
    assert query(database,u,'SELECT count(*) FROM billing_audit')==[(1,)]
    assert not s.pre_checkout(u,'q',identity,'XTR',100)
    assert 'Оплачено' in send(s,u,'/subscription').text
    later=payment(identity,charge=f'renew-{u}',end=datetime.now(timezone.utc)+timedelta(days=60));s.record_payment(u,later)
    s.record_payment(u,p)
    assert query(database,u,'SELECT max(period_end) FROM billing_payments')[0][0]==datetime.fromtimestamp(later['subscription_expiration_date'],timezone.utc)
    s.record_renewal(u,identity,False)
    assert 'Оплачено' in send(s,u,'/subscription').text
    s.record_payment(u,later,True);s.record_payment(u,later)
    assert query(database,u,'SELECT refunded FROM billing_payments WHERE charge_id=%s',(later['telegram_payment_charge_id'],))==[(True,)]


def test_refund_before_success_and_expired_read_only(service,database,billing):
    s=service;u=next(USERS)
    send(s,u,callback=draft(s,u,'100'))
    identity=invoice(s,u);p=payment(identity,charge=f'refundfirst-{u}')
    s.record_payment(u,p,True);s.record_payment(u,p)
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET trial_days=0')
    assert 'Доступ истёк' in send(s,u,'/subscription').text
    assert 'завершён' in send(s,u,'/manual 200').text
    assert '100,00' in send(s,u,'/history').text
    assert '100,00' in send(s,u,'/report').text
    assert send(s,u,'/csv').generated_document


def test_shared_access_uses_owner_and_admin_free(service,database,billing):
    s=service;owner,user=setup(s)
    own_invoice=invoice(s,owner);s.record_payment(owner,payment(own_invoice,charge=f'shared-{owner}'))
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET trial_days=0')
    assert 'Пока' not in send(s,user,'/manual 100').text  # amount accepted, next step category
    send(s,user,'/cancel')
    assert 'завершён' not in send(s,user,'/manual 100').text
    send(s,user,'/cancel');send(s,999,'/start')
    assert 'Бесплатный' in send(s,999,'/subscription').text
    assert 'завершён' not in send(s,999,'/manual 10').text


def test_quota_reservation_and_release(database,billing):
    s=Service(database[1],ai=FakeAI(error='timeout'));u=next(USERS)
    try:
        send(s,u,'/start')
        with s._actor_transaction(u) as c:c.execute('UPDATE user_settings SET ai_enabled=true WHERE user_id=actor_user_id()')
        send(s,u,'/add 10');send(s,u,'Кофе')
        assert query(database,u,'SELECT state FROM quota_reservations')==[('released',)]
        send(s,u,'/cancel');s.ai.error=None
        send(s,u,'/add 10');send(s,u,'Кофе')
        assert query(database,u,"SELECT count(*) FROM quota_reservations WHERE state='consumed'")==[(1,)]
        send(s,u,'/cancel');send(s,u,'/add 20')
        assert 'Квота' in send(s,u,'Другой кофе').text
        assert len(s.ai.calls)==2
        assert 'Квота' not in send(s,u,'/manual').text
    finally:s.close()


def test_concurrent_shared_quota_is_reserved_once(database,billing):
    s=Service(database[1],ai=FakeAI())
    try:
        owner,user=setup(s)
        for u in (owner,user):
            with s._actor_transaction(u) as c:c.execute('UPDATE user_settings SET ai_enabled=true WHERE user_id=actor_user_id()')
            send(s,u,'/add 100')
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda u:send(s,u,'Кофе'),(owner,user)))
        assert sum('Квота' in r.text for r in results)==1
        assert len(s.ai.calls)==1
        assert query(database,owner,"SELECT sum(units) FROM quota_reservations WHERE state IN ('reserved','consumed')")==[(1,)]
    finally:s.close()


def test_durable_inbox_duplicate_and_recovery(service,database):
    from aiogram.types import Update
    s=service;u=next(USERS);send(s,u,'/start')
    update=Update.model_validate({'update_id':991001,'message':{'message_id':1,'date':int(datetime.now(timezone.utc).timestamp()),'chat':{'id':u,'type':'private'},'from':{'id':u,'is_bot':False,'first_name':'Test'},'text':'/report'}})
    s.accept_updates(777,[update,update]);job=s.next_update(777)
    assert job['update_id']==update.update_id and not s.next_update(777)
    assert not query(database,u,'SELECT * FROM telegram_inbox')
    with psycopg.connect(database[0]) as c:
        c.execute("SET LOCAL balans.inbox_worker='on'");c.execute("UPDATE balans.telegram_inbox SET lease_until=now()-interval '1 second' WHERE bot_id=777")
    reclaimed=s.next_update(777)
    assert reclaimed['attempts']==2
    s.finish_update(777,update.update_id,reclaimed['lease_token'])
    assert not s.next_update(777)
    s.accept_updates(777,[update]);assert not s.next_update(777)


def test_reconciliation_does_not_invent_expiration(service,database,billing):
    s=service;u=next(USERS);identity=invoice(s,u);p=payment(identity,charge=f'recover-{u}')
    assert 'сверки' in s.reconcile_payment(u,p,False).text
    assert query(database,u,'SELECT count(*) FROM billing_payments')==[(0,)]
    assert query(database,u,'SELECT action FROM billing_audit')==[('reconciliation_missing_expiry',)]
    s.record_payment(u,p)
    assert 'подтверждена' in s.reconcile_payment(u,p,False).text


def test_refund_requires_operator_and_is_not_retried(service,database,billing):
    import asyncio
    from balans.refunds import execute_refund
    s=service;u=next(USERS);identity=invoice(s,u);p=payment(identity,charge=f'operator-refund-{u}');s.record_payment(u,p)
    with pytest.raises(ValueError):s.request_refund(u,p['telegram_payment_charge_id'],'Запрос пользователя')
    request=s.request_refund(999,p['telegram_payment_charge_id'],'Запрос пользователя')
    class FakeBot:
        calls=0
        async def refund_star_payment(self,actor,charge):
            assert actor==u and charge==p['telegram_payment_charge_id'];self.calls+=1;return True
    bot=FakeBot();assert 'возвращены' in asyncio.run(execute_refund(bot,s,999,request['id']))
    with pytest.raises(ValueError):asyncio.run(execute_refund(bot,s,999,request['id']))
    assert bot.calls==1
    assert query(database,u,'SELECT refunded FROM billing_payments')==[(True,)]
