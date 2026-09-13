"""Deliver saved support requests independently of financial processing."""
import asyncio
import logging

DEVELOPER_ID = 294966057


def claim(service):
    with service.pool.connection() as c, c.transaction():
        c.execute("SELECT set_config('balans.support_worker','on',true)")
        return c.execute("""WITH due AS (
          SELECT id FROM balans.support_tickets WHERE delivered_at IS NULL AND delivery_after<=now()
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE balans.support_tickets t SET delivery_after=now()+interval '5 minutes'
          FROM due WHERE t.id=due.id RETURNING t.id,t.telegram_user_id,t.body""").fetchone()


def complete(service, identity):
    with service.pool.connection() as c, c.transaction():
        c.execute("SELECT set_config('balans.support_worker','on',true)")
        c.execute('UPDATE balans.support_tickets SET delivered_at=now() WHERE id=%s',(identity,))


async def deliver_one(bot, service):
    ticket=await asyncio.to_thread(claim,service)
    if not ticket:return False
    await bot.send_message(DEVELOPER_ID,
        f"✉️ Обращение пользователя\nОтправитель: {ticket['telegram_user_id']}\n"
        f"Обращение: {ticket['id']}\n\n{ticket['body']}",parse_mode=None)
    await asyncio.to_thread(complete,service,ticket['id'])
    return True


async def support_loop(bot,service):
    while True:
        try:
            if await deliver_one(bot,service):continue
        except Exception as exc:
            logging.getLogger('balans.support').error('Ошибка доставки обращения (%s)',type(exc).__name__)
        await asyncio.sleep(5)
