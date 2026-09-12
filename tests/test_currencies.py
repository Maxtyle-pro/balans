from itertools import count
from decimal import Decimal
from io import BytesIO
import base64,csv
from pypdf import PdfReader
from test_service import send,query,draft
from test_receipts import button
from test_workspaces import admit

USERS=count(180000000)

def account(s,u,name='Доллары',currency='USD'):
    r=send(s,u,f'/account {name} | {currency}')
    return send(s,u,callback=button(r,'Использовать этот счёт'))

def confirm(s,u,r):return send(s,u,callback=next(data for row in r.buttons for label,data in row if label in ('Подтвердить','Сохранить изменения')))


def test_native_accounts_reports_and_export(service,database):
    s=service;u=next(USERS)
    send(s,u,callback=draft(s,u,'100'))
    account(s,u);send(s,u,callback=draft(s,u,'10'))
    history=send(s,u,'/history').text
    assert '10,00 USD' in history and '100,00 ₽' in history
    assert '10,00 USD' in send(s,u,'/report').text
    all_report=send(s,u,'/report all | currency=all')
    assert 'Валюты раздельно' in all_report.text and '110,00' not in all_report.text
    pdf=send(s,u,callback=button(all_report,'PDF'))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(pdf.generated_document))).pages)
    assert 'USD' in text and 'RUB' in text and '{currency}' not in text
    exported=send(s,u,'/csv all | currency=all')
    rows=list(csv.reader(base64.b64decode(exported.generated_document).decode('utf-8-sig').splitlines()))
    assert {r[4] for r in rows[1:]}=={'RUB','USD'}
    assert len(rows[0])==len(rows[1])==24
    sharing=send(s,u,callback=button(all_report,'Поделиться'))
    assert len(sharing.messages)==2 and 'USD' in ''.join(sharing.messages)
    assert 'одну валюту' in send(s,u,'/analyze all | currency=all').text


def test_manual_fx_snapshot_and_no_rate_conversion(service,database):
    s=service;u=next(USERS);send(s,u,'/start');account(s,u)
    assert 'сохранён' in send(s,u,'/fx USD | 95.5 | сегодня').text
    send(s,u,callback=draft(s,u,'10'))
    r=query(database,u,'SELECT amount,currency,base_amount,exchange_rate FROM operation_revisions')[0]
    assert r==(Decimal('10'), 'USD',Decimal('955'),Decimal('95.5'))
    send(s,u,'/fx USD | 100 | сегодня')
    assert query(database,u,'SELECT base_amount FROM operation_revisions')==[(Decimal('955'),)]
    report=send(s,u,'/report all | currency=all | convert=RUB')
    assert '955,00' in report.text and 'ручным' in report.text
    other=next(USERS);send(s,other,'/start');account(s,other);send(s,other,callback=draft(s,other,'10'))
    assert 'Нет сохранённого курса' in send(s,other,'/report all | currency=all | convert=RUB').text


def test_exchange_two_amounts_and_cancellation(service,database):
    s=service;u=next(USERS);send(s,u,'/start');confirm(s,u,send(s,u,'/opening 100'))
    account(s,u);send(s,u,callback=button(send(s,u,'/accounts'),'Основной'))
    preview=send(s,u,'/exchange 100 | Доллары | 1 | сегодня')
    assert '100,00 ₽' in preview.text and '1,00 USD' in preview.text
    confirm(s,u,preview)
    assert sorted(query(database,u,"SELECT currency,sum(delta) FROM postings GROUP BY currency"))==[('RUB',Decimal('0')),('USD',Decimal('1'))]
    report=send(s,u,'/report all | currency=all').text
    assert 'расходы 100,00' not in report
    assert query(database,u,"SELECT destination_amount FROM operation_revisions r JOIN operations o ON o.current_revision_id=r.id WHERE o.kind='transfer'")==[(Decimal('1'),)]
    identity=query(database,u,"SELECT id FROM operations WHERE kind='transfer'")[0][0]
    edit=send(s,u,callback=f'fedit:{identity}');edit=send(s,u,callback=button(edit,'Сумма зачисления'));edit=send(s,u,'1.1');confirm(s,u,edit)
    assert sorted(query(database,u,"SELECT currency,sum(delta) FROM postings GROUP BY currency"))==[('RUB',Decimal('0')),('USD',Decimal('1.1'))]
    send(s,u,callback=f'fdelete:{identity}');cancel=send(s,u,'Ошибочная запись');confirm(s,u,cancel)
    assert sorted(query(database,u,"SELECT currency,sum(delta) FROM postings GROUP BY currency"))==[('RUB',Decimal('100')),('USD',Decimal('0'))]


def test_shared_currency_and_foreign_text(service,database):
    s=service;owner,user=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки | USD');admit(s,owner,user)
    assert query(database,user,'SELECT DISTINCT currency FROM accounts')==[('USD',)]
    r=send(s,user,'Кофе 10 USD')
    assert 'Выберите категорию' in r.text
    send(s,user,'Продукты');card=send(s,user,'сегодня');assert '10,00 USD' in card.text
    send(s,user,'/cancel');send(s,user,'/batch cancel')
    assert 'не совпадает' in send(s,user,'Кофе 10 рублей').text
    assert 'валюту' in send(s,user,'/account Евро | EUR').text
