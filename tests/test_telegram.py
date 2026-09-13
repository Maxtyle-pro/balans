import asyncio
from datetime import datetime, timezone
from aiogram.types import Update
import pytest
from balans.__main__ import process_update


class FakeBot:
    id=42

    def __init__(self, fail=False):
        self.messages=[]
        self.fail=fail

    async def answer_callback_query(self, query_id):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            self.fail=False
            raise ConnectionError('Simulated disconnect after database commit')
        self.messages.append((chat_id,text,kwargs))


def update(number, text=None, chat_type='private', query=None):
    message={'message_id':number,'date':int(datetime.now(timezone.utc).timestamp()),
             'chat':{'id':555000 if chat_type=='private' else -100,'type':chat_type},
             'from':{'id':555000,'is_bot':False,'first_name':'Tester'}}
    if text is not None:
        message['text']=text
    if query:
        return Update.model_validate({'update_id':number,'callback_query':{
            'id':str(number),'from':message['from'],'chat_instance':'test',
            'message':message,'data':query}})
    return Update.model_validate({'update_id':number,'message':message})


def test_private_messages_and_retry(service):
    bot=FakeBot()
    for number,text in enumerate(['/manual 123,45','Дом','<b>Literal text</b>','сегодня'],start=50000):
        asyncio.run(process_update(bot,service,update(number,text)))
    markup=bot.messages[-1][2]['reply_markup']
    button=markup.inline_keyboard[0][0].callback_data
    bot.fail=True
    saving=update(50004,query=button)
    with pytest.raises(ConnectionError):
        asyncio.run(process_update(bot,service,saving))
    asyncio.run(process_update(bot,service,saving))
    asyncio.run(process_update(bot,service,update(50005,'/report')))
    assert 'Расходы — 123,45' in bot.messages[-1][1]
    assert bot.messages[-1][2].get('parse_mode') is None


def test_group_and_unsupported_input(service):
    bot=FakeBot()
    asyncio.run(process_update(bot,service,update(50006,'/history',chat_type='group')))
    assert bot.messages==[]
    asyncio.run(process_update(bot,service,update(50007)))
    assert 'ручной ввод' in bot.messages[-1][1]


def test_developer_button_starts_bot_input(service):
    from balans.__main__ import deliver_reply
    from balans.domain import Reply
    from balans.simple_interface import MAIN_BUTTONS
    bot=FakeBot()
    asyncio.run(deliver_reply(bot,555000,Reply('Меню',MAIN_BUTTONS)))
    rows=bot.messages[-1][2]['reply_markup'].inline_keyboard
    contact=next(b for row in rows for b in row if b.text=='✉️ Написать разработчику')
    assert contact.url is None
    assert contact.callback_data == 'ui:go:contact'
    assert rows[0][0].callback_data=='add'


def test_single_start_and_redelivery_send_one_reply(service):
    from aiogram.types import Update
    bot=FakeBot()
    bot.id=598001
    event=update(598001,'/start')
    service.accept_updates(bot.id,[event,event])
    job=service.next_update(bot.id)
    assert job is not None
    assert service.next_update(bot.id) is None
    asyncio.run(process_update(bot,service,Update.model_validate(job['payload'])))
    assert service.finish_update(bot.id,job['update_id'],job['lease_token'])
    service.accept_updates(bot.id,[event])
    assert service.next_update(bot.id) is None
    assert len(bot.messages)==1
    assert 'С чего начнём?' in bot.messages[0][1]
