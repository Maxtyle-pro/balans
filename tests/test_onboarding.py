from datetime import datetime, timedelta, timezone
from itertools import count
from concurrent.futures import ThreadPoolExecutor
import psycopg
import pytest

from test_service import send, query
from test_receipts import button, receipts
from test_billing import billing, start_trial, invoice, payment
from test_categorization import FakeAI
from balans.service import Service
from balans.planning import monthly_slot

USERS=count(210000000)


def test_start_terms_and_activation_once(database,billing):
    s=Service(database[1],ai=FakeAI());u=next(USERS)
    try:
        r=send(s,u,'/start')
        r=send(s,u,callback=button(r,'₽ Рубли'))
        assert len(r.text)<400 and not r.messages
        assert [label for row in r.buttons for label,_ in row]==['🎁 Начать 7 дней бесплатно','💡 Как пользоваться']
        assert query(database,u,'SELECT trial_started_at FROM billing_accounts')==[(None,)]
        assert 'Сначала начните' in send(s,u,'Кофе 250').text
        offer=send(s,u,callback=button(r,'🎁 Начать 7 дней бесплатно'))
        assert '100 Stars за 30 дней' in offer.text and 'автоматического списания нет' in offer.text
        assert not offer.invoice_id and not query(database,u,'SELECT id FROM billing_invoices')
        accept=button(offer,'Понятно, начать')
        assert query(database,u,'SELECT trial_started_at FROM billing_accounts')==[(None,)]
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:send(s,u,callback=accept),range(2)))
        assert sum('Начнём с первой записи' in r.text for r in results)==1
        started,until=query(database,u,'SELECT trial_started_at,trial_until FROM billing_accounts')[0]
        assert until-started==timedelta(days=7)
        assert query(database,u,"SELECT count(*) FROM billing_audit WHERE action='trial_started'")==[(1,)]
        assert query(database,u,'SELECT ai_enabled FROM user_settings')==[(True,)]
        assert query(database,u,'SELECT enabled,monthly FROM notification_preferences')==[(True,True)]
        send(s,u,callback=accept)
        assert query(database,u,'SELECT trial_until FROM billing_accounts')==[(until,)]
        assert '➖ Записать расход' in [x for row in send(s,u,'/start').buttons for x,_ in row]
        r=send(s,u,'Кофе 250');assert len(s.ai.calls)==1
        r=send(s,u,callback=button(r,'Подтвердить категорию'))
        r=send(s,u,callback=button(r,'Сохранить'))
        assert 'сохранён' in r.text
        assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
        card=send(s,u,'/subscription')
        assert 'ИИ-распознавания текста: 0 из 1' in card.text
        assert not query(database,u,'SELECT charge_id FROM billing_payments')
    finally:s.close()


def test_changed_offer_requires_new_acceptance(service,database,billing):
    u=next(USERS);s=service
    offer=send(s,u,callback='trialinfo');old=button(offer,'Понятно, начать')
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET stars=101')
    try:
        r=send(s,u,callback=old)
        assert 'Условия изменились' in r.text and '101 Stars' in r.text
        assert query(database,u,'SELECT trial_started_at FROM billing_accounts')==[(None,)]
        assert 'Начнём' in send(s,u,callback=button(r,'Понятно, начать')).text
    finally:
        with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET stars=100')


def test_trial_keeps_deadline_and_snapshot_when_tariff_changes(service,database,billing):
    u=next(USERS);s=service;start_trial(s,u)
    before=query(database,u,'SELECT trial_until,trial_quotas FROM billing_accounts')[0]
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.billing_config SET trial_days=20,trial_text_quota=500')
    r=send(s,u,'/subscription')
    assert 'ИИ-распознавания текста: 1 из 1' in r.text
    assert query(database,u,'SELECT trial_until,trial_quotas FROM billing_accounts')[0]==before


def test_disabled_billing_and_short_help(service,database):
    u=next(USERS);r=send(service,u,'/start')
    r=send(service,u,callback=button(r,'₽ Рубли'))
    assert 'бесплатно' not in ' '.join(x for row in r.buttons for x,_ in row)
    assert '👋 <b>С чего начнём?</b>' in r.text
    assert r.buttons == [[('➖ Записать расход','add')], [('➕ Записать доход','ui:go:income')], [('☰ Меню','ui:menu')]]
    r=send(service,u,callback=button(r,'☰ Меню'))
    assert '💡 Помощь' not in [x for row in r.buttons for x,_ in row]
    assert len(r.text)<700 and not r.messages
    assert '⭐ Подписка' in [x for row in r.buttons for x,_ in row]
    assert 'пока не включена' in send(service,u,callback='trialinfo').text
    assert not query(database,u,'SELECT trial_started_at FROM billing_accounts WHERE trial_started_at IS NOT NULL')


def test_expired_trial_cannot_restart(service,database,billing):
    s=service;u=next(USERS);offer=send(s,u,callback='trialinfo');accept=button(offer,'Понятно, начать')
    send(s,u,callback=accept)
    with s._actor_transaction(u) as c:c.execute("UPDATE billing_accounts SET trial_until=now()-interval '1 second' WHERE user_id=actor_user_id()")
    assert 'Доступ истёк' in send(s,u,callback=accept).text
    assert 'завершён' in send(s,u,'/manual 100').text
    assert send(s,u,'/csv').generated_document


def test_subscription_uses_paid_period_and_separate_analysis_quota(database,billing):
    s=Service(database[1],ai=FakeAI());u=next(USERS)
    try:
        start_trial(s,u);send(s,u,'Кофе 250');send(s,u,'/cancel')
        assert 'ИИ-распознавания текста: 0 из 1' in send(s,u,'/subscription').text
        identity=invoice(s,u);s.record_payment(u,payment(identity,charge=f'period-{u}'))
        assert 'ИИ-распознавания текста: 1 из 1' in send(s,u,'/subscription').text
        send(s,u,'Кофе 500');send(s,u,'/cancel')
        assert 'ИИ-распознавания текста: 0 из 1' in send(s,u,'/subscription').text
        with s._actor_transaction(u) as c:
            report=s._report_snapshot(c,query(database,u,'SELECT id FROM users')[0][0],'',datetime.now(timezone.utc))
            c.execute("INSERT INTO report_jobs(workspace_id,author_user_id,report_id,kind) VALUES(current_workspace(),actor_user_id(),%s,'analysis')",(report['id'],))
        assert query(database,u,"SELECT units FROM quota_reservations WHERE kind='analysis'")==[(1,)]
        assert 'ИИ-анализы отчётов: 29 из 30' in send(s,u,'/subscription').text
    finally:s.close()


def test_monthly_timezone_year_boundary():
    now=datetime(2027,1,1,5,tzinfo=timezone.utc)
    assert monthly_slot(now,'Europe/Moscow',9*60)==datetime(2026,12,1,6,tzinfo=timezone.utc)
    assert monthly_slot(now+timedelta(hours=1),'Europe/Moscow',9*60)==datetime(2027,1,1,6,tzinfo=timezone.utc)


def test_monthly_report_dedup_and_explicit_disable(service,database):
    s=service;u=next(USERS)
    send(s,u,'/start')
    assert not query(database,u,'SELECT id FROM notification_preferences')
    with s._actor_transaction(u) as c:
        s._preference(c)
        c.execute('UPDATE notification_preferences SET quiet_start=NULL,quiet_end=NULL WHERE user_id=actor_user_id()')
    now=datetime.now(timezone.utc)
    with s._actor_transaction(u) as c:
        c.execute("UPDATE notification_preferences SET enabled_at=now()-interval '40 days',monthly_enabled_at=now()-interval '40 days',send_minute=0,next_check_at=now()-interval '1 minute' WHERE user_id=actor_user_id()")
    s.plan_notifications(now);s.plan_notifications(now+timedelta(minutes=2))
    rows=query(database,u,"SELECT id,message FROM notification_outbox WHERE kind='monthly'")
    assert len(rows)==1 and 'Итоги месяца' in rows[0][1]
    notice=next(j for j in s.claim_notifications(now+timedelta(minutes=3)) if j['id']==rows[0][0])
    assert s.prepare_notification(u,notice['id'],now+timedelta(minutes=3))
    assert 'Расходы —' in send(s,u,callback=f"nopen:{notice['id']}").text
    send(s,u,callback='monthlyoff')
    assert s.prepare_notification(u,notice['id'],now+timedelta(minutes=3)) is None
    assert query(database,u,"SELECT count(*) FROM report_jobs WHERE kind='analysis'")==[(0,)]


def test_renewal_resets_quota_but_calendar_month_does_not(database,billing):
    s=Service(database[1],ai=FakeAI());u=next(USERS)
    try:
        identity=invoice(s,u)
        end=datetime.now(timezone.utc)+timedelta(days=15)
        s.record_payment(u,payment(identity,charge=f'first-{u}',end=end))
        send(s,u,'/ai on')
        send(s,u,'Кофе 250');send(s,u,'/cancel')
        assert 'ИИ-распознавания текста: 0 из 1' in send(s,u,'/subscription').text
        # Calendar bucket is deliberately different; entitlement period remains unchanged.
        with psycopg.connect(database[0]) as c:
            c.execute("SET LOCAL balans.billing_worker='on'")
            c.execute("UPDATE balans.quota_reservations SET created_at=now()-interval '1 month' WHERE sponsor_id=(SELECT user_id FROM balans.billing_accounts WHERE telegram_user_id=%s)",(u,))
        assert 'ИИ-распознавания текста: 0 из 1' in send(s,u,'/subscription').text
        s.record_payment(u,payment(identity,charge=f'next-{u}'))
        assert 'ИИ-распознавания текста: 1 из 1' in send(s,u,'/subscription').text
    finally:s.close()


def test_shared_participant_sees_owner_usage_and_cannot_activate_owner(service,database,billing):
    from test_funds import setup
    s=service;owner,member=setup(s)
    card=send(s,member,callback='trialinfo')
    assert 'владелец' in card.text and not any(data.startswith('trialaccept:') for row in card.buttons for _,data in row)
    offer=send(s,owner,callback='trialinfo')
    assert 'владелец' in send(s,member,callback=button(offer,'Понятно, начать')).text
    start_trial(s,owner)
    owner_card=send(s,owner,'/subscription');member_card=send(s,member,'/subscription')
    assert 'ИИ-распознавания текста: 1 из 1' in owner_card.text and 'ИИ-распознавания текста: 1 из 1' in member_card.text
    assert 'владелец' in send(s,member,callback='billdetails').text


def test_pdf_quota_counts_files_after_success(receipts,database,billing):
    from receipt_fixtures import pdf_bytes
    s,ai,_=receipts;u=next(USERS);start_trial(s,u)
    r=s.receive_receipt(u,42,90001001,datetime.now(timezone.utc),'pages',pdf_bytes(pages=3),'document')
    r=send(s,u,callback=button(r,'Распознать один чек'))
    assert len(ai.calls)==1
    assert query(database,u,"SELECT units FROM quota_reservations WHERE kind='image'")==[(1,)]
    send(s,u,'/cancel')
    r=s.receive_receipt(u,42,90001002,datetime.now(timezone.utc),'pages2',pdf_bytes(pages=2),'document')
    send(s,u,callback=button(r,'Распознать один чек'))
    assert len(ai.calls)==2
    assert query(database,u,"SELECT sum(units) FROM quota_reservations WHERE kind='image'")==[(2,)]
