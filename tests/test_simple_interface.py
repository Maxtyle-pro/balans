import re
from itertools import count
from test_service import send,query
from test_receipts import button,receipts
from test_money_recognition import TextFake,enable
from balans.command_ui import present_reply
from balans.__main__ import COMMANDS

USERS=count(360000000)

def readable(reply):
    reply=present_reply(reply)
    labels=' '.join(label for row in reply.buttons for label,_ in row)
    assert not re.search(r'(?<![\w/<])/[a-z]+',reply.text),reply.text
    assert not re.search(r'\bсч[её]т\w*',reply.text+' '+labels,re.I),reply.text+' '+labels
    assert not any(word in labels.lower() for word in ('google','общий бюджет','участник','сверка'))
    return reply

def test_simple_menu_and_settings_no_accounts_or_commands(service):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake()
    for cb in ('start','ui:menu','ui:go:settings','ui:section:recognition','ui:go:ai','ui:go:voice','ui:go:receipts','ui:go:privacy','balance','history','report','subscription'):
        readable(send(s,u,callback=cb))
    assert COMMANDS==[('start','Открыть главное меню')]

def test_buttons_income_opening_expense_and_edit(service,database):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake('expense','339')
    readable(send(s,u,callback='ui:go:opening'))
    assert 'Начальный остаток записан' in readable(send(s,u,'1000')).text
    readable(send(s,u,callback='ui:go:income'))
    assert 'Доход записан' in readable(send(s,u,'500')).text
    readable(send(s,u,callback='add'))
    expense=readable(send(s,u,'Вода 339'))
    assert 'Расход записан' in expense.text
    edit=readable(send(s,u,callback=button(expense,'✏️ Изменить')))
    readable(send(s,u,callback=button(edit,'Сумма')))
    preview=readable(send(s,u,'300'))
    readable(send(s,u,callback=button(preview,'Сохранить изменения')))
    assert '1 200,00' in readable(send(s,u,callback='balance')).text
    assert query(database,u,'SELECT count(*) FROM operations')==[(3,)]

def test_stale_account_and_sheets_buttons_are_closed(service):
    from uuid import uuid4
    s=service;u=next(USERS);enable(s,u)
    for cb in ('accounts','ui:go:account','ui:go:workspace','workspaces','ui:go:sheets_connect','sverify:'+str(uuid4()),'rsask:'+str(uuid4())):
        reply=send(s,u,callback=cb)
        assert not any('Создать счёт' in label or 'Google' in label for row in reply.buttons for label,_ in row)

def test_old_ui_input_does_not_capture_next_operation(service,database):
    s=service;u=next(USERS);enable(s,u);s.text_ai=TextFake('income')
    send(s,u,callback='ui:go:budgetday')
    send(s,u,callback='ui:go:settings')
    assert query(database,u,'SELECT count(*) FROM ui_inputs')==[(0,)]
    assert 'Доход записан' in send(s,u,'Получил 5000').text


def test_period_and_timezone_are_buttons(service):
    s=service;u=next(USERS);enable(s,u)
    picker=readable(send(s,u,callback='ui:go:settings_zone'))
    assert 'Москва' in str(picker.buttons)
    assert 'Нью-Йорк' in readable(send(s,u,callback='tz:America/New_York')).text
    picker=readable(send(s,u,callback='ui:go:report_period'))
    readable(send(s,u,callback=button(picker,'Сегодня')))


def test_addressed_sheets_command_cannot_connect(service,database):
    s=service;u=next(USERS);enable(s,u)
    for command in ('/sheets@balans_bot connect test','/SHEETS connect test'):
        assert 'отключено' in send(s,u,command).text
    assert query(database,u,'SELECT count(*) FROM sheets_connections')==[(0,)]


def test_automatic_features_have_no_settings_switches(service):
    u=next(USERS)
    enable(service,u)
    settings=send(service,u,callback='ui:go:settings')
    labels=' '.join(label for row in settings.buttons for label,_ in row)
    assert 'Уведомления' not in labels and 'Распознавание' not in labels
    for cb in ('ui:section:recognition','ui:go:ai','ui:go:voice','ui:go:receipts','monthlysettings','monthlyoff','ui:go:notify','ui:go:notify_off','ai_off'):
        r=send(service,u,callback=cb)
        assert r.buttons==[[('☰ Меню','ui:menu')]]
        assert 'автоматически' in r.text


def test_ai_on_and_off_commands_reach_text_ai_settings(service,database):
    u=next(USERS)
    assert 'выключено' in send(service,u,'/ai off').text
    assert query(database,u,'SELECT ai_enabled FROM user_settings')==[(False,)]
    assert 'включено' in send(service,u,'/ai on').text
    assert query(database,u,'SELECT ai_enabled FROM user_settings')==[(True,)]
