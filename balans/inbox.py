"""Durable intake, ordered per actor, with fenced worker leases."""
import asyncio
from psycopg.types.json import Jsonb

class Inbox:
    def accept_updates(self,bot_id,updates):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            for update in updates:
                query=update.callback_query
                message=query.message if query else update.message
                actor=query.from_user if query else message.from_user if message else None
                if not actor or actor.is_bot or not message or message.chat.type!='private' or message.chat.id!=actor.id:continue
                c.execute('INSERT INTO balans.telegram_inbox(bot_id,update_id,actor_telegram_id,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(bot_id,update.update_id,actor.id,Jsonb(update.model_dump(mode='json',exclude_none=True))))

    def album_has_more(self,bot_id,actor,update_id,group_id):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            return c.execute("SELECT 1 FROM balans.telegram_inbox WHERE bot_id=%s AND actor_telegram_id=%s AND update_id>%s AND state='pending' AND payload->'message'->>'media_group_id'=%s LIMIT 1",(bot_id,actor,update_id,group_id)).fetchone() is not None

    def next_update(self,bot_id):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            # Short DB-only lock avoids races between concurrent SKIP LOCKED claimers.
            c.execute('SELECT pg_advisory_xact_lock(%s)',(bot_id,))
            c.execute("UPDATE balans.telegram_inbox SET state='pending',lease_token=NULL WHERE bot_id=%s AND state='processing' AND lease_until<now()",(bot_id,))
            return c.execute("""WITH due AS (
              SELECT i.bot_id,i.update_id FROM balans.telegram_inbox i
              WHERE i.bot_id=%s AND i.state='pending' AND i.available_at<=now()
              AND NOT EXISTS(SELECT 1 FROM balans.telegram_inbox earlier WHERE earlier.bot_id=i.bot_id
                AND earlier.actor_telegram_id=i.actor_telegram_id AND earlier.state IN ('pending','processing')
                AND (earlier.state='processing' OR earlier.update_id<i.update_id))
              ORDER BY i.available_at,i.update_id FOR UPDATE SKIP LOCKED LIMIT 1)
              UPDATE balans.telegram_inbox i SET state='processing',attempts=attempts+1,
              lease_token=gen_random_uuid(),lease_until=now()+interval '10 minutes'
              FROM due WHERE i.bot_id=due.bot_id AND i.update_id=due.update_id RETURNING i.*""",(bot_id,)).fetchone()

    def finish_update(self,bot_id,update_id,lease_token,error=None):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            return c.execute("""UPDATE balans.telegram_inbox SET state=CASE WHEN %s::text IS NULL THEN 'done' WHEN attempts>=10 THEN 'failed' ELSE 'pending' END,
              error_code=%s,available_at=now()+least(attempts*5,60)*interval '1 second',
              completed_at=CASE WHEN %s::text IS NULL THEN now() ELSE NULL END,
              payload=CASE WHEN %s::text IS NULL THEN '{}'::jsonb ELSE payload END,lease_token=NULL,lease_until=NULL
              WHERE bot_id=%s AND update_id=%s AND state='processing' AND lease_token=%s""",(error,error,error,error,bot_id,update_id,lease_token)).rowcount==1

    def renew_update(self,bot_id,update_id,lease_token):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            return c.execute("UPDATE balans.telegram_inbox SET lease_until=now()+interval '10 minutes' WHERE bot_id=%s AND update_id=%s AND state='processing' AND lease_token=%s",(bot_id,update_id,lease_token)).rowcount==1

    def queue_health(self,bot_id):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.inbox_worker','on',true)")
            return c.execute("""SELECT count(*) FILTER(WHERE state IN ('pending','processing')) AS queued,
              count(*) FILTER(WHERE state='failed') AS failed,
              coalesce(extract(epoch FROM now()-min(created_at) FILTER(WHERE state IN ('pending','processing'))),0)::float AS oldest_seconds
              FROM balans.telegram_inbox WHERE bot_id=%s""",(bot_id,)).fetchone()

async def inbox_loop(bot,service,processor,stop=None):
    from aiogram.types import Update
    from aiogram.exceptions import TelegramForbiddenError
    import logging
    log=logging.getLogger('balans.inbox');stop=stop or asyncio.Event()
    while not stop.is_set():
        try:job=await asyncio.to_thread(service.next_update,bot.id)
        except Exception as exc:
            log.error('Ошибка входящей очереди (%s)',type(exc).__name__)
            await asyncio.sleep(1);continue
        if not job:
            try:await asyncio.wait_for(stop.wait(),.1)
            except TimeoutError:pass
            continue
        error=None
        active=asyncio.Event()
        async def heartbeat():
            while not active.is_set():
                try:await asyncio.wait_for(active.wait(),30)
                except TimeoutError:
                    try:await asyncio.to_thread(service.renew_update,bot.id,job['update_id'],job['lease_token'])
                    except Exception as exc:log.error('Ошибка продления обработки (%s)',type(exc).__name__)
        lease=asyncio.create_task(heartbeat())
        try:await processor(bot,service,Update.model_validate(job['payload']))
        except TelegramForbiddenError:
            if job['actor_telegram_id']:await asyncio.to_thread(service.notifications_blocked,job['actor_telegram_id'])
        except Exception as exc:
            error=type(exc).__name__;log.error('Ошибка обработки входящего события (%s)',error)
        finally:
            active.set();await lease
        try:await asyncio.to_thread(service.finish_update,bot.id,job['update_id'],job['lease_token'],error)
        except Exception as exc:
            log.error('Ошибка фиксации входящего события (%s)',type(exc).__name__)
            await asyncio.sleep(1)
