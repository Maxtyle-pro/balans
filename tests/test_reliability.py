import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import psycopg
from aiogram.types import Update
from aiohttp.test_utils import TestClient,TestServer
from balans.transport import make_app


def update(i,user=190000001):
    return Update.model_validate({'update_id':i,'message':{'message_id':i,'date':int(datetime.now(timezone.utc).timestamp()),'chat':{'id':user,'type':'private'},'from':{'id':user,'is_bot':False,'first_name':'Test'},'text':'/report'}})


def test_parallel_claims_order_and_stale_completion(service,database):
    bot=18001;service.accept_updates(bot,[update(1),update(2),update(3,190000002)])
    with ThreadPoolExecutor(4) as pool:jobs=list(pool.map(lambda _:service.next_update(bot),range(4)))
    jobs=[j for j in jobs if j];assert sorted(j['update_id'] for j in jobs)==[1,3]
    first=next(j for j in jobs if j['update_id']==1)
    with psycopg.connect(database[0]) as c:
        c.execute("SET LOCAL balans.inbox_worker='on'");c.execute("UPDATE balans.telegram_inbox SET lease_until=now()-interval '1 second' WHERE bot_id=%s AND update_id=1",(bot,))
    renewed=service.next_update(bot);assert renewed['update_id']==1
    assert not service.renew_update(bot,1,first['lease_token'])
    assert service.renew_update(bot,1,renewed['lease_token'])
    assert not service.finish_update(bot,1,first['lease_token'])
    assert service.finish_update(bot,1,renewed['lease_token'])
    assert service.next_update(bot)['update_id']==2


def test_retry_does_not_overtake_actor(service):
    bot=18002;service.accept_updates(bot,[update(1),update(2),update(3,190000002)])
    first=service.next_update(bot);service.finish_update(bot,1,first['lease_token'],'TimeoutError')
    assert service.next_update(bot)['update_id']==3
    assert service.next_update(bot) is None


def test_webhook_authentication_durable_ack_and_checkout():
    class Bot:id=18
    class Service:
        accepted=[]
        def accept_updates(self,bot,updates):self.accepted.extend(updates)
        def queue_health(self,bot):return {}
    async def run():
        service=Service();checkout=[]
        async def processor(bot,s,u):checkout.append(u.update_id)
        async with TestClient(TestServer(make_app(Bot(),service,processor,'a'*32))) as client:
            assert (await client.post('/telegram',json=update(1).model_dump(mode='json'))).status==403
            headers={'X-Telegram-Bot-Api-Secret-Token':'a'*32}
            assert (await client.post('/telegram',json=update(1).model_dump(mode='json'),headers=headers)).status==200
            assert [u.update_id for u in service.accepted]==[1]
            q={'update_id':2,'pre_checkout_query':{'id':'q','from':{'id':1,'is_bot':False,'first_name':'T'},'currency':'XTR','total_amount':1,'invoice_payload':'invoice'}}
            assert (await client.post('/telegram',json=q,headers=headers)).status==200
            assert checkout==[2] and len(service.accepted)==1
            assert (await client.get('/ready')).status==200
            assert (await client.post('/telegram',data='bad',headers=headers)).status==400
    asyncio.run(run())


def test_stop_drains_current_job():
    from balans.inbox import inbox_loop
    async def run():
        stop=asyncio.Event();started=asyncio.Event();release=asyncio.Event();finished=[]
        class Bot:id=1
        class Service:
            claimed=False
            def next_update(self,bot):
                if self.claimed:return None
                self.claimed=True
                return {'update_id':1,'payload':update(1).model_dump(mode='json'),'lease_token':'test','actor_telegram_id':190000001}
            def finish_update(self,*args):finished.append(args)
        async def processor(*args):started.set();await release.wait()
        task=asyncio.create_task(inbox_loop(Bot(),Service(),processor,stop))
        await started.wait();stop.set();await asyncio.sleep(.01)
        assert not task.done() and not finished
        release.set();await asyncio.wait_for(task,2)
        assert len(finished)==1 and finished[0][2]=='test'
    asyncio.run(run())
