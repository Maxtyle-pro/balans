from itertools import count
from decimal import Decimal
import base64
import pytest
from test_service import send,query,NOW
from test_automatic_capture import enable
from test_receipts import receipts,review,button
from test_media_flow import extracted,transaction,category
from receipt_fixtures import photo_bytes,pdf_bytes

USERS=count(390000000)
UPDATES=count(390000000)

def edit(s,u,card):
    return send(s,u,callback=button(card,'✏️ Изменить'))

def hidden_action(reply,value):
    """Exercise retained advanced callbacks without exposing them in the editor UI."""
    _,draft_id,version=button(reply,'Отмена').split(':')
    return f'faction:{value}:{draft_id}:{version}'

def test_type_change_reverses_journal_and_keeps_history(service,database):
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Кофе 250 сегодня')
    picker=send(s,u,callback=hidden_action(edit(s,u,card),'type'))
    changed=send(s,u,callback=button(picker,'Доход'))
    assert query(database,u,'SELECT kind FROM operations')==[('expense',)]
    saved=send(s,u,callback=button(changed,'Сохранить изменения'))
    assert 'Доход записан' in saved.text
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('250'),)]
    assert query(database,u,'SELECT operation_kind FROM operation_revisions ORDER BY revision_no')==[('expense',),('income',)]
    picker=send(s,u,callback=hidden_action(edit(s,u,saved),'type'))
    changed=send(s,u,callback=button(picker,'Расход'))
    send(s,u,callback=button(changed,'Сохранить изменения'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-250'),)]

def test_delete_after_edit_is_confirmed_and_idempotent(service,database):
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Кофе 250 сегодня')
    editor=edit(s,u,card)
    confirmation=send(s,u,callback=hidden_action(editor,'delete'))
    assert query(database,u,'SELECT state FROM operations')==[('active',)]
    confirm=button(confirmation,'Сохранить изменения')
    send(s,u,callback=confirm);send(s,u,callback=confirm)
    assert query(database,u,'SELECT state FROM operations')==[('cancelled',)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('0'),)]

def test_attach_after_edit_keeps_one_operation_and_can_download(service,database):
    s=service;u=next(USERS);enable(s,u)
    card=send(s,u,'Кофе 250 сегодня')
    prompt=send(s,u,callback=hidden_action(edit(s,u,card),'attach'))
    assert 'Новая операция не создаётся' in prompt.text
    result=s.receive_receipt(u,42,next(UPDATES),NOW,'new-file',pdf_bytes())
    assert 'сохранён' in result.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT count(*) FROM receipt_batches')==[(0,)]
    original=send(s,u,callback=button(result,'Скачать оригинал 1'))
    assert base64.b64decode(original.generated_document).startswith(b'%PDF')

def test_negative_opening_and_reopen_existing(service,database):
    s=service;u=next(USERS);enable(s,u)
    send(s,u,callback='ui:go:opening');card=send(s,u,'-1000')
    assert '-1 000,00' in card.text
    history=send(s,u,'/history')
    assert '-1 000,00' in history.text
    send(s,u,callback=button(history,'🔎 Найти'))
    assert 'Начальный остаток' in send(s,u,'Начальный остаток').text
    reopened=send(s,u,callback='ui:go:opening')
    assert 'уже задан' in reopened.text
    assert 'Сохранить изменения' not in [label for row in reopened.buttons for label,_ in row]
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]

def test_text_then_screenshot_requires_duplicate_decision(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    send(s,u,'Покупка 150 сегодня')
    card=extracted(s,ai,u,[transaction(category(database,u))])
    assert 'Возможный дубль' in card.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    link=next(data for row in card.buttons for label,data in row if label.startswith('Прикрепить к'))
    preview=send(s,u,callback=link)
    send(s,u,callback=button(preview,'Прикрепить без нового расхода'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT count(*) FROM documents')==[(1,)]

def test_text_then_receipt_requires_duplicate_decision(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    from test_receipts import NOW as RECEIPT_DATE
    send(s,u,'Покупка 150 '+RECEIPT_DATE.strftime('%d.%m.%Y'))
    card=review(s,u)
    assert 'Возможный дубль' in card.text
    link=next(data for row in card.buttons for label,data in row if label.startswith('Прикрепить к'))
    send(s,u,callback=link)
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]
    assert query(database,u,'SELECT count(*) FROM documents')==[(1,)]

def test_saved_receipt_split_is_atomic_and_replay_safe(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    card=review(s,u)
    prompt=send(s,u,callback=hidden_action(edit(s,u,card),'split'))
    assert 'Баланс не изменится' in prompt.text
    confirm=button(prompt,'Подтвердить разбиение')
    result=send(s,u,callback=confirm)
    assert result.text.startswith('✅ Расход записан'),result.text
    assert len(result.additional_replies)==1
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(2,)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-150'),)]
    send(s,u,callback=confirm)
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(2,)]
    review(s,u)
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(2,)]
    clear=send(s,u,callback='historyclear')
    result=send(s,u,callback=button(clear,'🗑 Удалить всю историю'))
    assert 'История очищена' in result.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]

def test_failed_split_keeps_original(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u);card=review(s,u)
    editor=edit(s,u,card)
    query(database,u,"UPDATE receipt_batches SET result=jsonb_set(result,'{items_complete}','false') RETURNING id")
    result=send(s,u,callback=hidden_action(editor,'split'))
    assert 'полный список' in result.text
    assert query(database,u,'SELECT state FROM operations')==[('active',)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-150'),)]

def test_ai_toggle_route_and_privacy_navigation(service,database):
    s=service;u=next(USERS);enable(s,u)
    settings=send(s,u,callback='ui:go:settings')
    privacy=send(s,u,callback=button(settings,'🔒 Данные и приватность'))
    assert button(privacy,'Удалить профиль')=='ui:go:delete'
    send(s,u,'/ai off')
    disabled=send(s,u,'Кофе 250')
    panel=send(s,u,callback=button(disabled,'Настройки ИИ'))
    send(s,u,callback=button(panel,'Включить'))
    assert query(database,u,'SELECT ai_enabled FROM user_settings')==[(True,)]
    for channel,column in [('voice','voice_enabled'),('receipts','receipts_enabled')]:
        send(s,u,'/'+channel+' off')
        assert query(database,u,'SELECT '+column+' FROM user_settings')==[(False,)]
        panel=send(s,u,callback='ui:go:'+channel)
        send(s,u,callback=button(panel,'Включить'))
        assert query(database,u,'SELECT '+column+' FROM user_settings')==[(True,)]

def test_report_refresh_and_csv(service,database):
    s=service;u=next(USERS);enable(s,u);card=send(s,u,'Кофе 250 сегодня')
    report=send(s,u,'/report')
    assert 'Снимок на' in report.text
    csv=send(s,u,callback=button(report,'📑 CSV за период'))
    assert csv.generated_filename.endswith('.csv')
    editor=edit(s,u,card);send(s,u,callback=button(editor,'Изменить сумму'))
    preview=send(s,u,'300');send(s,u,callback=button(preview,'Сохранить изменения'))
    refreshed=send(s,u,callback=button(report,'🔄 Обновить'))
    assert 'Расходы — 300,00' in refreshed.text
    assert 'Расходы — 250,00' in report.text

def test_split_failure_mid_save_rolls_back_every_operation(receipts,database,monkeypatch):
    s,ai,_=receipts;u=next(USERS);enable(s,u);card=review(s,u)
    preview=send(s,u,callback=hidden_action(edit(s,u,card),'split'))
    save=s._media_save
    def fail_second(c,q,index):
        if index==1:raise ValueError('Проверочная ошибка сохранения')
        return save(c,q,index)
    monkeypatch.setattr(s,'_media_save',fail_second)
    result=send(s,u,callback=button(preview,'Подтвердить разбиение'))
    assert 'Проверочная ошибка' in result.text
    assert query(database,u,'SELECT state FROM operations')==[('active',)]
    assert query(database,u,'SELECT count(*) FROM operation_revisions')==[(1,)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-150'),)]

def test_media_split_reupload_does_not_restore_parent(receipts,database):
    s,ai,_=receipts;u=next(USERS);enable(s,u);cat=category(database,u)
    row=transaction(cat,items_complete=True)
    row['items']=[dict(name='Хлеб',quantity='1',unit_price='100',line_total='100',discount='0'),dict(name='Вода',quantity='1',unit_price='50',line_total='50',discount='0')]
    card=extracted(s,ai,u,[row])
    preview=send(s,u,callback=hidden_action(edit(s,u,card),'split'))
    send(s,u,callback=button(preview,'Подтвердить разбиение'))
    again=extracted(s,ai,u,[row])
    assert 'уже записан' in again.text
    assert len(again.additional_replies)==1
    assert query(database,u,"SELECT count(*) FROM operations WHERE state='active'")==[(2,)]
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-150'),)]
    assert query(database,u,'SELECT DISTINCT source_kind FROM operation_revisions')==[('screenshot',)]

def test_type_change_does_not_break_refund_or_other_users(service,database):
    from test_finance import confirm
    s=service;u=next(USERS);other=next(USERS);enable(s,u)
    card=send(s,u,'Кофе 250 сегодня')
    identity=button(card,'✏️ Изменить').split(':')[1]
    send(s,u,callback='frefund:'+identity)
    confirm(s,u,send(s,u,'50'))
    editor=edit(s,u,card)
    assert 'устарела' in send(s,other,callback=hidden_action(editor,'delete')).text
    types=send(s,u,callback=hidden_action(editor,'type'))
    changed=send(s,u,callback=button(types,'Доход'))
    error=send(s,u,callback=button(changed,'Сохранить изменения'))
    assert 'возвраты' in error.text
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-200'),)]
    assert sorted(query(database,u,'SELECT kind FROM operations'))==[('expense',),('refund',)]

def test_hidden_workspace_commands_blocked_at_telegram_boundary(service,database):
    import asyncio
    from test_telegram import FakeBot,update as event
    from aiogram.types import Update
    from balans.__main__ import process_update
    s=service;bot=FakeBot();u=next(USERS)
    def update(number,text=None,query=None):
        data=event(number,text,query=query).model_dump()
        message=data['callback_query']['message'] if query else data['message']
        message['chat']['id']=u;message['from_user']['id']=u
        if query:data['callback_query']['from_user']['id']=u
        return Update.model_validate(data)
    asyncio.run(process_update(bot,s,update(next(UPDATES),'/workspace Скрытый')))
    assert 'Старые разделы' in bot.messages[-1][1]
    assert query(database,u,"SELECT count(*) FROM workspaces WHERE kind='shared'")==[(0,)]
    # Simulate a workspace selected by an earlier version.
    send(s,u,'/workspace Прежний')
    shared=str(query(database,u,"SELECT id FROM workspaces WHERE kind='shared'")[0][0])
    asyncio.run(process_update(bot,s,update(next(UPDATES),'Покупка 150')))
    assert 'Вернитесь' in bot.messages[-1][1] or 'Перейдите' in bot.messages[-1][1]
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    asyncio.run(process_update(bot,s,update(next(UPDATES),query='personalbudget')))
    assert query(database,u,'SELECT kind FROM workspaces WHERE id=current_workspace()')==[('personal',)]
    asyncio.run(process_update(bot,s,update(next(UPDATES),query='wsuse:'+shared)))
    assert query(database,u,'SELECT kind FROM workspaces WHERE id=current_workspace()')==[('personal',)]
    assert query(database,u,"SELECT count(*) FROM workspaces WHERE kind='shared'")==[(1,)]

def test_real_user_does_not_get_demo_upsells_or_currency_dead_end(service,database):
    s=service;u=next(USERS);enable(s,u);send(s,u,'Кофе 250 сегодня')
    currency=send(s,u,callback='currencysettings')
    assert 'недоступно' in currency.text
    assert not any(data.startswith('usercurrency:') for row in currency.buttons for _,data in row)
    with s._actor_transaction(u) as c:
        for card in (s._quota_card(c,'image'),s._storage_overview(c),s._subscription_card(c)):
            assert not any(data.startswith(('addon:','extend:')) for row in card.buttons for _,data in row)

@pytest.mark.parametrize('source',['receipt','screenshot'])
def test_future_file_date_uses_upload_date_without_warning(receipts,database,source):
    from datetime import timedelta
    from test_receipts import NOW as RECEIPT_NOW
    s,ai,_=receipts;u=next(USERS);enable(s,u)
    if source=='receipt':
        ai.changes={'occurred_on':(NOW+timedelta(days=30)).date().isoformat()}
        card=review(s,u);expected=RECEIPT_NOW.date()
    else:
        item=transaction(category(database,u));item['occurred_on']=(NOW+timedelta(days=30)).date().isoformat()
        card=extracted(s,ai,u,[item]);expected=NOW.date()
    assert '📅 '+expected.strftime('%d.%m.%Y') in card.text
    assert 'дата сообщения' not in card.text
    assert query(database,u,'SELECT occurred_on,capture_warnings FROM operation_revisions')==[(expected,[])]

def test_quota_notification_has_no_unavailable_purchase(service,database):
    s=service;u=next(USERS);enable(s,u)
    with s._actor_transaction(u) as c:
        c.execute('UPDATE notification_preferences SET quiet_start=NULL,quiet_end=NULL WHERE user_id=actor_user_id()')
        identity=c.execute("INSERT INTO notification_outbox(workspace_id,user_id,telegram_user_id,kind,entity_kind,message,event_key,state) VALUES(current_workspace(),actor_user_id(),actor_telegram_id(),'quota','image','Лимит ИИ исчерпан. Можно добавить пакет или выбрать расширенный тариф. Ручной ввод остаётся доступен.','ux_quota','sending') RETURNING id").fetchone()['id']
    n=s.prepare_notification(u,identity)
    assert n and not n['addons_available']
    assert 'добавить пакет' not in n['message'] and 'Ручной ввод' in n['message']
