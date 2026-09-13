from itertools import count
from test_service import send,query,draft,NOW
from test_receipts import receipts,button,review,save
from test_voice import voices,audio
from balans.service import Service

USERS=count(320000000)

def test_currency_choice_at_start_persists_and_new_accounts_inherit(service,database):
    s=service;u=next(USERS)
    start=send(s,u,'/start');assert 'Выберите валюту' in start.text
    reply=send(s,u,callback=button(start,'$ Доллары'));assert 'USD' in reply.text
    assert query(database,u,'SELECT currency,currency_selected_at IS NOT NULL FROM user_settings')==[('USD',True)]
    assert query(database,u,'SELECT currency FROM accounts')==[('USD',)]
    send(s,u,'/account Наличные')
    assert query(database,u,'SELECT DISTINCT currency FROM accounts')==[('USD',)]
    restarted=Service(database[1])
    try:assert 'Выберите валюту' not in send(restarted,u,'/start').text
    finally:restarted.close()
    assert button(send(s,u,'/settings'),'💱 Валюта')=='currencysettings'
    send(s,u,callback=draft(s,u,'100'))
    assert 'не изменены' in send(s,u,callback='usercurrency:EUR').text
    assert query(database,u,'SELECT currency,amount FROM operation_revisions')==[('USD',100)]


def test_receipt_missing_currency_uses_selected_currency(receipts,database):
    s,ai,_=receipts;u=next(USERS)
    send(s,u,'/start');send(s,u,callback='usercurrency:USD')
    ai.changes={'currency':None}
    card=review(s,u)
    assert 'Валюта чека не подтверждена' not in card.text
    assert query(database,u,'SELECT receipt_currency FROM operation_drafts')==[('USD',)]
    result=save(s,u,card)
    assert 'сохранён' in result.text
    assert query(database,u,'SELECT currency FROM operation_revisions')==[('USD',)]


def test_explicit_foreign_receipt_is_not_relabelled(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');send(s,u,callback='usercurrency:RUB')
    ai.changes={'currency':'EUR'}
    card=review(s,u)
    assert 'EUR' in card.text and 'не сохранён' in card.text
    assert not query(database,u,'SELECT id FROM operations')


def test_voice_missing_currency_uses_setting(voices,database):
    s,ai=voices;u=next(USERS);send(s,u,'/start');send(s,u,callback='usercurrency:EUR')
    ai.changes={'currency':None}
    card=s.receive_voice(u,42,990000001,NOW,audio())
    assert query(database,u,'SELECT a.currency FROM operation_drafts d JOIN accounts a ON a.id=d.account_id')==[('EUR',)]
    assert 'EUR' in card.text


def test_existing_media_queue_missing_currency_uses_setting(receipts,database):
    from test_media_flow import extracted,transaction,category
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');send(s,u,callback='usercurrency:RUB')
    extracted(s,ai,u,[transaction(category(database,u))])
    query(database,u,"UPDATE media_queues SET items=jsonb_set(items,'{0,currency}','null') RETURNING id")
    send(s,u,'/media')
    assert query(database,u,"SELECT items->0->>'currency' FROM media_queues WHERE state='active'")==[('RUB',)]
