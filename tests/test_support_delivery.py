import asyncio
from test_service import send,query
from test_telegram import FakeBot
from balans.support_delivery import deliver_one,DEVELOPER_ID


def test_contact_delivery_and_cancel(service,database):
    user=389001
    send(service,user,'/start')
    prompt=send(service,user,callback='ui:go:contact')
    assert 'текстом' in prompt.text
    assert service.awaiting_contact(user)
    result=send(service,user,'Кофе 250 — ошибка распознавания')
    assert 'Сообщение принято' in result.text
    assert not service.awaiting_contact(user)
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    bot=FakeBot()
    assert asyncio.run(deliver_one(bot,service))
    assert bot.messages[0][0]==DEVELOPER_ID
    assert 'Кофе 250' in bot.messages[0][1]
    assert not asyncio.run(deliver_one(bot,service))
    send(service,user,callback='ui:go:contact')
    send(service,user,callback='ui:menu')
    assert not service.awaiting_contact(user)


def test_failed_delivery_is_kept_for_retry(service,database):
    import pytest
    user=389002
    send(service,user,'/support Проверка повторной доставки')
    bot=FakeBot(fail=True)
    with pytest.raises(ConnectionError):asyncio.run(deliver_one(bot,service))
    assert query(database,user,'SELECT delivered_at FROM support_tickets')==[(None,)]
    assert not asyncio.run(deliver_one(bot,service))
    query(database,user,"UPDATE support_tickets SET delivery_after=now()-interval '1 second' RETURNING id")
    assert asyncio.run(deliver_one(bot,service))
    assert len(bot.messages)==1
