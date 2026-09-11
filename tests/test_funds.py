from itertools import count
from decimal import Decimal
from test_service import send,query,draft
from test_receipts import button
from test_workspaces import admit

USERS=count(100000000)

def setup(s):
    owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    return owner,user

def issue(s,owner,user,amount='20000'):
    r=send(s,owner,f'/issue {user} | {amount} | сегодня | Закупки')
    return send(s,owner,callback=button(r,'Деньги отправлены'))

def identity(db,owner):return str(query(db,owner,'SELECT id FROM fund_transfers ORDER BY created_at DESC LIMIT 1')[0][0])

def confirm(s,user,r):return send(s,user,callback=button(r,'Подтвердить'))

def test_issue_partial_and_return_accounting(service,database):
    s=service;owner,user=setup(s)
    opening=send(s,owner,'/opening 20000');confirm(s,owner,opening)
    planned=send(s,owner,f'/issue {user} | 20000 | сегодня | Закупки')
    assert query(database,owner,'SELECT count(*) FROM fund_entries')==[(0,)]
    sent=send(s,owner,callback=button(planned,'Деньги отправлены'))
    assert 'В пути' in sent.text
    assert 'устарела' in send(s,owner,callback=button(planned,'Деньги отправлены')).text
    transfer=identity(database,owner)
    partial=send(s,user,f'/receive {transfer} | 12000');confirm(s,user,partial)
    assert '8 000,00' in send(s,owner,'/funds').text
    assert '12 000,00' in send(s,user,'/accounts').text
    assert 'Повторной записи нет' in confirm(s,user,partial).text
    confirm(s,user,send(s,user,f'/receive {transfer} | 8000'))
    send(s,user,callback=draft(s,user,'6500'))
    back=send(s,user,'/returnfunds 3000 | сегодня | Остаток')
    send(s,user,callback=button(back,'Деньги отправлены'))
    assert '10 500,00' in send(s,user,'/accounts').text
    assert 'Сверенный остаток: 10 500,00' in send(s,owner,'/funds summary').text
    returned=identity(database,owner)
    confirm(s,owner,send(s,owner,f'/receive {returned} | 3000'))
    assert query(database,owner,'SELECT sum(delta) FROM fund_entries')==[(Decimal('0'),)]
    assert '6 500,00' in send(s,owner,'/report').text
    assert query(database,owner,"SELECT count(*) FROM operations WHERE kind IN ('income','transfer')")==[(0,)]


def test_claim_external_rejection_and_match(service,database):
    s=service;owner,user=setup(s)
    r=send(s,user,'/claim 1000 | сегодня | Заказчик | Материалы');confirm(s,user,r)
    assert 'заявленный: 1 000,00' in send(s,user,'/accounts').text
    cl=str(query(database,owner,'SELECT id FROM fund_claims')[0][0])
    r=send(s,owner,f'/reconcile {cl} | external | Выписка проверена');confirm(s,owner,r)
    assert '1 000,00 ₽ · сверенный' in send(s,user,'/accounts').text
    assert 'Доходы: 1 000,00' in send(s,user,'/report').text
    confirm(s,user,send(s,user,'/claim 500 | сегодня | Руководитель | Новая выдача'))
    cl=str(query(database,owner,"SELECT id FROM fund_claims WHERE state='pending'")[0][0])
    issue(s,owner,user,'500');tr=identity(database,owner)
    confirm(s,owner,send(s,owner,f'/reconcile {cl} | matched | Сверено | {tr}'))
    assert '1 500,00 ₽ · сверенный' in send(s,user,'/accounts').text
    assert query(database,owner,'SELECT sum(delta) FROM fund_entries')==[(Decimal('1000'),)]
    confirm(s,user,send(s,user,'/claim 99 | сегодня | Ошибка | Проверка'))
    cl=str(query(database,owner,"SELECT id FROM fund_claims WHERE state='pending'")[0][0])
    confirm(s,owner,send(s,owner,f'/reconcile {cl} | rejected | Дубль'))
    assert query(database,owner,"SELECT reason FROM fund_claims WHERE state='rejected'")==[('Дубль',)]


def test_funds_authorization_and_dispute(service,database):
    s=service;owner,user=setup(s);other=next(USERS);admit(s,owner,other)
    issue(s,owner,user,'700');tr=identity(database,owner)
    assert 'недоступна' in send(s,other,'/funds '+tr).text
    assert query(database,other,'SELECT * FROM fund_entries')==[]
    assert 'недоступна' in send(s,owner,f'/receive {tr} | 700').text
    confirm(s,user,send(s,user,f'/dispute {tr} | Получил не всю сумму'))
    assert 'Получил не всю сумму' in send(s,owner,'/funds '+tr).text
    excessive=send(s,user,f'/receive {tr} | 701')
    assert 'не превышать' in confirm(s,user,excessive).text
    assert query(database,owner,'SELECT sum(delta) FROM fund_entries')==[(Decimal('0'),)]
    assert 'сопоставьте' in send(s,user,'/claim 700 | сегодня | Руководитель | Выдача').text


def test_plan_cancel_future_due_and_expired_confirmation(service,database):
    s=service;owner,user=setup(s)
    r=send(s,owner,f'/issue {user} | 500 | сегодня | Закупки | 01.01.2030')
    assert '2030-01-01' in r.text
    send(s,owner,callback=button(r,'Отменить план'))
    assert query(database,owner,'SELECT count(*) FROM fund_entries')==[(0,)]
    r=send(s,user,'/claim 50 | сегодня | Клиент | Аванс')
    with s._actor_transaction(user) as c:c.execute("UPDATE fund_confirmations SET expires_at=now()-interval '1 second'")
    assert 'истекло' in confirm(s,user,r).text
    assert query(database,owner,'SELECT count(*) FROM fund_claims')==[(0,)]


def test_concurrent_receipt_is_once(service,database):
    from concurrent.futures import ThreadPoolExecutor
    s=service;owner,user=setup(s);issue(s,owner,user,'99')
    tr=identity(database,owner);r=send(s,user,f'/receive {tr} | 99')
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda _:confirm(s,user,r),range(2)))
    assert any('Повторной записи нет' in r.text for r in replies)
    assert query(database,owner,"SELECT sum(delta) FROM fund_entries WHERE account_id IS NOT NULL AND delta>0")==[(Decimal('99'),)]
    assert query(database,owner,'SELECT sum(delta) FROM fund_entries')==[(Decimal('0'),)]
