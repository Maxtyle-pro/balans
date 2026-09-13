from dataclasses import asdict
from uuid import UUID
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from balans.domain import Reply
from balans.text_ai import TextResult
from balans.voice_ai import validate_result,mask

class TextRecognition:
    def _text_recognize(self,c,user,text,sent):
        if len(text)>2000:return Reply('Отправьте до 2000 символов и 8 операций.')
        if not self.text_ai.available or not c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()['ai_enabled']:
            return Reply('Для распознавания текста нужен включённый ИИ. Можно добавить запись вручную.',[[('Настройки ИИ','ui:go:ai')],[('Доход','ui:go:income'),('Начальный остаток','ui:go:opening')],[('Расход вручную','ui:go:manual')]])
        if quota:=self._quota_preflight(c,'text'):return quota
        c.execute("UPDATE text_jobs SET state='failed',reply=%s,error_code='interrupted' WHERE state='running' AND lease_until<now()",(Jsonb(asdict(Reply('Обработка прервалась. Отправьте сообщение снова.'))),))
        if c.execute("SELECT 1 FROM text_jobs WHERE state IN ('queued','running')").fetchone():return Reply('Предыдущее сообщение ещё распознаётся.',[[('Отменить','ui:go:cancel')]])
        row=self._account_context(c)
        request={'message':mask(text),'message_date':sent.astimezone(ZoneInfo(row['timezone'])).date().isoformat(),'user_currency':row['currency'],'categories':[{'id':str(x['id']),'name':x['name']} for x in self._categories(c,row['id'])]}
        job=c.execute("INSERT INTO text_jobs(workspace_id,author_user_id,account_id,source_sent_at,timezone_snapshot,request,model) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",(row['id'],user,row['account_id'],sent,row['timezone'],Jsonb(request),self.text_ai.model)).fetchone()
        return Reply('Распознаю запись…',text_job_id=str(job['id']))

    def _resolve_text_job(self,actor,identity):
        with self._actor_transaction(actor) as c:
            cached=c.execute('SELECT reply,workspace_id FROM text_jobs WHERE id=%s',(UUID(identity),)).fetchone()
            current=c.execute('SELECT current_workspace() AS id').fetchone()['id']
        if cached and cached['workspace_id']!=current:return Reply('Вернитесь в бюджет, из которого отправили сообщение.',[[('Мои бюджеты','ui:go:workspaces')]])
        if cached and cached['reply']:return self._resolve_text_reply(actor,Reply(**cached['reply']))
        with self._actor_transaction(actor) as c:
            job=c.execute('SELECT *,lease_until>now() AS leased FROM text_jobs WHERE id=%s',(UUID(identity),)).fetchone()
            if not job:return Reply('Распознавание недоступно.')
            if job['reply']:
                reply=Reply(**job['reply'])
                return reply
            if job['state'] not in ('queued','running'):return Reply('Срок хранения результата истёк. История записей доступна в меню.',[[('История','history')]])
            if job['state']=='running':
                if job['leased']:return Reply('Текст ещё распознаётся. Дождитесь результата.')
                reply=Reply('Обработка прервалась. Отправьте сообщение снова.',[[('Меню','ui:menu')]])
                c.execute("UPDATE text_jobs SET state='failed',reply=%s,error_code='interrupted' WHERE id=%s",(Jsonb(asdict(reply)),job['id']));return reply
            if job['workspace_id']!=c.execute('SELECT current_workspace() AS id').fetchone()['id']:return Reply('Вернитесь в бюджет, из которого отправили сообщение.')
            c.execute("UPDATE text_jobs SET state='running',lease_until=now()+interval '2 minutes' WHERE id=%s",(job['id'],))
        try:
            extracted,inp,out=self.text_ai.extract(job['request'])
            result=TextResult.model_validate(extracted)
            entries=[validate_result(x,job['request']['categories'],job['request']['message_date']) for x in result.operations]
            error=None
        except Exception as exc:
            entries=[];inp=out=None;error=type(exc).__name__
        with self._actor_transaction(actor) as c:
            live=c.execute('SELECT state,reply FROM text_jobs WHERE id=%s',(job['id'],)).fetchone()
            if not live or live['state']!='running':return Reply(**live['reply']) if live and live['reply'] else Reply('Распознавание отменено.')
            enabled=c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()['ai_enabled']
            if not enabled or job['workspace_id']!=c.execute('SELECT current_workspace() AS id').fetchone()['id']:
                reply=Reply('Распознавание отменено: изменились настройки или бюджет. Ничего не записано.');error='cancelled'
            elif error:
                reply=Reply('Не удалось распознать текст. Ничего не записано. Повторите сообщение или используйте ручной ввод.',[[('Ввести вручную','ui:go:manual')]])
            elif not entries or any(x.kind in ('other','multiple') for x in entries):
                error='no_operations'
                reply=Reply('Не удалось выделить совершённые операции. Уточните описание и сумму. Для внесения денег можно выбрать:',[[('Доход','ui:go:income'),('Начальный остаток','ui:go:opening')]])
            elif any(x.currency not in (None,job['request']['user_currency']) for x in entries):
                reply=Reply('Валюта сообщения отличается от валюты учёта. Ничего не записано.',[[('Валюта','currencysettings')]])
            elif self._draft(c) or self._input_batch(c) or self._media_queue(c):
                reply=Reply('Сначала завершите текущую запись, затем отправьте текст повторно.',[[('Продолжить','ui:resume')]])
            elif any(x.kind in ('income','opening','incoming') for x in entries) and c.execute("SELECT kind='shared' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']:
                reply=Reply('Поступление в общий бюджет требует сверки.',[[('Выдачи и сверка','ui:go:funds')]])
            else:
                items=[{'kind':x.kind,'amount':x.amount,'description':x.description,'date':x.occurred_on,'category_id':x.category_id,'recognized':True,'capture_warnings':([] if x.occurred_on else ['date'])+([] if x.description else ['description'])} for x in entries]
                batch=c.execute('INSERT INTO input_batches(workspace_id,author_user_id,account_id,timezone_snapshot,source_sent_at,items) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',(job['workspace_id'],job['author_user_id'],job['account_id'],job['timezone_snapshot'],job['source_sent_at'],Jsonb(items))).fetchone()
                reply=Reply('Записываю операции…',capture_batch_id=str(batch['id']))
            c.execute('UPDATE text_jobs SET state=%s,reply=%s,result=%s,error_code=%s,input_tokens=%s,output_tokens=%s WHERE id=%s',('failed' if error else 'ready',Jsonb(asdict(reply)),Jsonb([x.model_dump() for x in entries]),error,inp,out,job['id']))
        return self._resolve_text_reply(actor,reply)

    def _resolve_text_reply(self,actor,reply):
        return self._resolve_capture_batch(actor,reply.capture_batch_id) if reply.capture_batch_id else reply
