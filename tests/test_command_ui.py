from itertools import count
from balans.command_ui import ACTIONS, present_reply
from balans.domain import Reply
from balans.__main__ import COMMANDS
from balans.service import Service
from test_service import send, query
from test_receipts import button, receipts
from test_media_flow import extracted, transaction, category, open_row, paid, save_row

USERS=count(270000000)


def test_every_published_command_has_button():
    assert {name for name,_ in COMMANDS} <= ACTIONS.keys()
    assert all(len(('ui:go:'+key).encode()) <= 64 for key in ACTIONS)


def test_presentation_preserves_text_and_exposes_commands():
    original=Reply('Сначала /media; /media cancel. Далее /history 2. Кофе <500>.')
    result=present_reply(original)
    assert result.text==original.text and original.buttons==[]
    assert button(result,'Продолжить список')=='ui:go:media'
    assert button(result,'Отменить список')=='ui:go:media_cancel'
    assert button(result,'История · страница 2')=='ui:go:history:2'
    assert present_reply(result).buttons==result.buttons


def test_menu_and_persistent_command_free_input(service,database):
    u=next(USERS)
    menu=send(service,u,'/help')
    assert not menu.messages
    section=send(service,u,callback=button(menu,'💳 Счета и бюджеты'))
    prompt=send(service,u,callback=button(section,'Создать счёт'))
    assert 'название' in prompt.text
    restarted=Service(database[1])
    try:r=send(restarted,u,'Кошелёк для теста')
    finally:restarted.close()
    assert 'Счёт создан' in r.text
    assert query(database,u,"SELECT name FROM accounts WHERE name='Кошелёк для теста'")==[('Кошелёк для теста',)]
    assert query(database,u,'SELECT count(*) FROM ui_inputs')==[(0,)]
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]


def test_input_cancel_scope_and_validation_retry(service,database):
    u=next(USERS);other=next(USERS)
    old=send(service,u,callback='ui:go:account')
    current=send(service,u,callback='ui:go:budgetday')
    send(service,other,callback=button(current,'Отмена'))
    send(service,u,callback=button(old,'Отмена'))
    assert query(database,u,'SELECT action FROM ui_inputs')==[('budgetday',)]
    send(service,u,'40')
    assert query(database,u,'SELECT action FROM ui_inputs')==[('budgetday',)]
    send(service,u,'5')
    assert query(database,u,'SELECT budget_start_day FROM user_settings')==[(5,)]


def test_media_buttons_preserve_saved_and_reject_old_queue(receipts,database):
    s,ai,_=receipts;u=next(USERS);send(s,u,'/start');cat=category(database,u)
    listing=extracted(s,ai,u,[transaction(cat),transaction(cat,'250')])
    save_row(s,u,paid(s,u,open_row(s,u,listing)))
    blocked=send(s,u,callback='add')
    assert '/media' not in blocked.text
    resume=send(s,u,callback=button(blocked,'Продолжить список'))
    assert 'Распознанные позиции' in resume.text
    cancel=button(blocked,'Отменить список')
    assert 'Сохранённые операции' in send(s,u,callback=cancel).text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    extracted(s,ai,u,[transaction(cat,'350')])
    assert 'недоступен' in send(s,u,callback=cancel).text
    assert query(database,u,"SELECT count(*) FROM media_queues WHERE state='active'")==[(1,)]


def test_transport_buttons_for_primary_and_extra_messages():
    import asyncio
    from test_telegram import FakeBot
    from balans.__main__ import deliver_reply
    bot=FakeBot()
    asyncio.run(deliver_reply(bot,42,Reply('Откройте /media.',messages=['Настройки: /voice.'])))
    first=bot.messages[0][2]['reply_markup'].inline_keyboard
    second=bot.messages[1][2]['reply_markup'].inline_keyboard
    assert any(b.callback_data=='ui:go:media' for row in first for b in row)
    assert any(b.callback_data=='ui:go:voice' for row in second for b in row)
