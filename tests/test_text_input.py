from datetime import datetime,timezone
from itertools import count
from decimal import Decimal
import pytest
from balans.text_input import parse_items
from test_service import send,query
from test_receipts import button

NOW=datetime(2026,9,10,22,tzinfo=timezone.utc)
USERS=count(90000000)


@pytest.mark.parametrize('text,value,day',[('Кофе 250 вчера','250','2026-09-10'),('Продукты 2,5 тыс','2500','2026-09-11'),('На такси 1 200,50 ₽ вчера','1200.50','2026-09-10')])
def test_parse(text,value,day):
    r=parse_items(text,NOW,'Europe/Moscow')[0]
    assert Decimal(r['amount'])==Decimal(value) and r['date']==day


@pytest.mark.parametrize('text',['1e3 кофе','кофе -250','планирую 1000 на продукты','кофе 100 или 200','доллары 500','аптека 1.001','без суммы'])
def test_ambiguous_rejected(text):
    with pytest.raises(ValueError):parse_items(text,NOW,'Europe/Moscow')


def test_single_and_multiple(service,database):
    s=service;u=next(USERS)
    card=send(s,u,'Кофе 250 вчера')
    assert 'категори' in card.text
    card=send(s,u,callback=button(card,'Кафе и рестораны'))
    send(s,u,callback=button(card,'Сохранить'))
    batch=send(s,u,'Обед 600; метро 70; аптека 430')
    assert '1 100,00' in batch.text
    assert len(query(database,u,'SELECT * FROM operations'))==1
    card=send(s,u,callback=button(batch,'Проверить №1'))
    card=send(s,u,callback=button(card,'Кафе и рестораны'))
    send(s,u,callback=button(card,'Сохранить'))
    batch=send(s,u,'/batch')
    assert 'сохранено' in batch.text
    batch=send(s,u,callback=button(batch,'Исключить №2'))
    assert 'исключено' in batch.text
    send(s,u,'/batch cancel')
    assert len(query(database,u,'SELECT * FROM operations'))==2


def test_own_categories_archive_history_and_search(service,database):
    s=service;u=next(USERS);other=next(USERS)
    send(s,u,'/categories add Автомобиль')
    card=send(s,u,'Бензин 1500')
    assert button(card,'Автомобиль')
    card=send(s,u,callback=button(card,'Автомобиль'));send(s,u,callback=button(card,'Сохранить'))
    send(s,u,'/categories rename Автомобиль | Машина')
    assert 'Машина' in send(s,u,'/history').text
    assert 'Бензин' in send(s,u,'/search бенз').text
    assert 'Бензин' in send(s,u,'/history type=expense | category=Машина').text
    send(s,u,'/categories archive Машина')
    card=send(s,u,'Бензин 500')
    assert all(label!='Машина' for row in card.buttons for label,_ in row)
    assert 'Бензин' not in send(s,other,'/search бенз').text
    assert query(database,other,"SELECT count(*) FROM category_events WHERE category_id NOT IN (SELECT id FROM categories WHERE name='Одежда')")==[(0,)]


def test_batch_access_and_default_account_snapshot(service,database):
    s=service;u=next(USERS);other=next(USERS)
    batch=send(s,u,'Кофе 100; Такси 200')
    assert 'недоступен' in send(s,other,callback=button(batch,'Проверить №1')).text
    card=send(s,u,'/account Карта');send(s,u,callback=button(card,'Использовать этот счёт'))
    card=send(s,u,callback=button(batch,'Проверить №1'));card=send(s,u,callback=button(card,'Продукты'))
    assert 'Счёт:' not in card.text


def test_income_phrase(service,database):
    s=service;u=next(USERS);card=send(s,u,'Зарплата 120000')
    assert 'Доход' in card.text
    send(s,u,callback=button(card,'Подтвердить'))
    assert query(database,u,'SELECT kind FROM operations')==[('income',)]


def test_batch_edit_preserves_item_and_double_click(service,database):
    s=service;u=next(USERS);batch=send(s,u,'Кофе 100; Такси 200')
    callback=button(batch,'Проверить №1')
    card=send(s,u,callback=callback)
    assert send(s,u,callback=callback).buttons==card.buttons
    card=send(s,u,callback=button(card,'Продукты'))
    card=send(s,u,callback=button(card,'Изменить'))
    send(s,u,callback=button(card,'Сумма'));card=send(s,u,'150')
    send(s,u,callback=button(card,'Подтвердить'))
    batch=send(s,u,'/batch')
    assert '350,00' in batch.text and '150,00' in batch.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
