"""Isolated DB load profile; synthetic AI and Telegram, no external requests."""
import argparse,asyncio,json,os,secrets,time
from datetime import datetime,timezone
from pathlib import Path
import psycopg
from psycopg import sql
from aiogram.types import Update
from balans.service import Service
from balans.inbox import inbox_loop
from balans.receipt_media import ReceiptStorage
from scripts.dev_setup import provision

async def exercise(service,seconds,rate,workers):
    class Bot:id=19001
    stop=asyncio.Event();start=time.perf_counter();received={};latencies={k:[] for k in ('text','photo','screenshot','terminal','voice')};ack=[];errors=[]
    kinds=['text']*6+['photo','screenshot','terminal','voice']
    async def processor(bot,s,update):
        kind=kinds[(update.update_id-1)%10]
        if kind=='text':
            reply=await asyncio.to_thread(s.handle,update.message.from_user.id,bot.id,update.update_id,'/manual 100',update.message.date)
            if not reply.text:errors.append(update.update_id)
        else:
            # Only queue capacity for media. These durations are controlled fixtures, not measured AI.
            await asyncio.sleep(.05 if kind!='voice' else .1)
        latencies[kind].append(time.perf_counter()-received[update.update_id])
    tasks=[asyncio.create_task(inbox_loop(Bot(),service,processor,stop)) for _ in range(workers)]
    total=seconds*rate
    try:
        for i in range(1,total+1):
            await asyncio.sleep(max(0,start+(i-1)/rate-time.perf_counter()))
            actor=210000000+(i-1)%1000
            u=Update.model_validate({'update_id':i,'message':{'message_id':i,'date':int(datetime.now(timezone.utc).timestamp()),'chat':{'id':actor,'type':'private'},'from':{'id':actor,'is_bot':False,'first_name':'Synthetic'},'text':'/manual 100'}})
            received[i]=time.perf_counter();await asyncio.to_thread(service.accept_updates,Bot.id,[u]);ack.append(time.perf_counter()-received[i])
            if i%(rate*30)==0:print(json.dumps({'accepted':i,'completed':sum(map(len,latencies.values()))}),flush=True)
        deadline=time.perf_counter()+120
        while sum(map(len,latencies.values()))<total and time.perf_counter()<deadline:await asyncio.sleep(.1)
    finally:
        stop.set();await asyncio.gather(*tasks)
    def stats(values):
        ordered=sorted(values)
        return {'count':len(values),'p95_seconds':round(ordered[min(len(ordered)-1,int(.95*len(ordered)))],4) if ordered else None,'max_seconds':round(max(values),4) if values else None}
    result={'registered':10000,'active_users':1000,'rate_per_second':rate,'duration_seconds':seconds,'workers':workers,'intake':stats(ack),'results':{k:stats(v) for k,v in latencies.items()},'queue':service.queue_health(Bot.id),'errors':len(errors),'synthetic_external_services':True,'measurement':'durable DB intake to internal result; excludes HTTP/TLS and Telegram; media AI replaced by fixed delay'}
    result['passed']=len(ack)==total and sum(map(len,latencies.values()))==total and not errors and result['queue']['failed']==0 and result['intake']['p95_seconds']<=2 and all(v['p95_seconds']<=({'text':10,'voice':45}.get(k,30)) for k,v in result['results'].items())
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=300);p.add_argument('--rate',type=int,default=20);p.add_argument('--workers',type=int,default=4);p.add_argument('--output',default='output/load-test.json');args=p.parse_args()
    if not 1<=args.seconds<=300 or not 1<=args.rate<=20 or not 1<=args.workers<=16:raise ValueError('Параметры превышают профиль теста.')
    admin=os.getenv('POSTGRES_ADMIN_DSN','dbname=postgres host=localhost');name='balans_load_'+secrets.token_hex(5)
    owner_dsn,runtime_dsn,owner,runtime=provision(admin,name)
    import tempfile
    try:
        print('Создана изолированная БД; регистрация 10 000 синтетических пользователей.',flush=True)
        with psycopg.connect(runtime_dsn) as c:
            c.execute('SET search_path=balans,public')
            for actor in range(210000000,210010000):
                c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(actor),))
                c.execute('SELECT balans.bootstrap()')
                if (actor-210000000+1)%1000==0:
                    c.commit()
                    print(json.dumps({'registered':actor-210000000+1}),flush=True)
                    with psycopg.connect(owner_dsn) as stats:stats.execute('ANALYZE')
        with tempfile.TemporaryDirectory(prefix='balans-load-') as tmp:
            s=Service(runtime_dsn,receipt_storage=ReceiptStorage(Path(tmp)/'receipts'))
            try:result=asyncio.run(exercise(s,args.seconds,args.rate,args.workers))
            finally:s.close()
        output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2))
        if not result['passed']:raise SystemExit(1)
    finally:
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in (runtime,owner):c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

if __name__=='__main__':main()
