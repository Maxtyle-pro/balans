"""Durable voice intake; external requests never run inside a DB transaction."""
from dataclasses import asdict
from uuid import UUID
from zoneinfo import ZoneInfo
import logging
from psycopg.types.json import Jsonb
from balans.domain import Reply, amount_from_text, date_from_text, money
from balans.voice_ai import mask, validate_result
from balans.voice_media import prepare_voice
from balans.receipt_media import MediaError
from balans.ai import match_rule

log = logging.getLogger('balans.voice')


class Voice:
    def _voice_disabled(self):
        return Reply('Обработка голоса выключена. /voice on — включить; /manual — ручной ввод.')

    def _voice_busy(self, c):
        c.execute("UPDATE voice_jobs SET state='failed',error_code='interrupted' WHERE state='processing' AND lease_until<=now()")
        return c.execute("SELECT id FROM voice_jobs WHERE state='processing'").fetchone() is not None

    def _voice_gate(self, c):
        if self._document_upload(c):return Reply('Сначала завершите прикрепление документа или /cancel.')
        if self._media_queue(c):return Reply('Сначала завершите список изображений: /media.')
        if not c.execute('SELECT voice_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()['voice_enabled']:
            return self._voice_disabled()
        if not self.voice_ai.available:
            return Reply('Распознавание голоса временно недоступно. /manual — ручной ввод.')
        c.execute("UPDATE operation_drafts SET state='cancelled' WHERE state='pending' AND expires_at<=now()")
        c.execute("UPDATE receipt_batches SET state='cancelled' WHERE state='collecting' AND expires_at<=now()")
        if self._draft(c) or c.execute("SELECT id FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone():
            return Reply('Сначала завершите текущий расход или чек. /add — черновик; /cancel — отмена.')
        if self._voice_busy(c):
            return Reply('Предыдущее голосовое сообщение обрабатывается. /voice — состояние; /cancel — отмена.')

    def _voice_cached(self, c, reply):
        if not reply.voice_job_id:
            return reply
        job = c.execute('SELECT *,lease_until>now() AS live FROM voice_jobs WHERE id=%s', (UUID(reply.voice_job_id),)).fetchone()
        if not job or job['state']=='cancelled':
            return Reply('Обработка голоса отменена.')
        draft=c.execute('SELECT state FROM operation_drafts WHERE voice_job_id=%s',(job['id'],)).fetchone()
        if draft and draft['state']!='pending':
            return Reply('Этот голосовой расход уже сохранён.' if draft['state']=='saved' else 'Черновик голосового расхода отменён.')
        if job['reply']:
            return Reply(**job['reply'])
        if job['state']=='processing' and job['live']:
            return Reply('Голос ещё обрабатывается. /voice — проверить результат; /cancel — отменить.')
        c.execute("UPDATE voice_jobs SET state='failed',error_code='interrupted' WHERE id=%s", (job['id'],))
        return Reply('Обработка голоса прервалась. Отправьте запись заново или используйте /manual. Автоматического повторного AI-запроса нет.')

    def voice_preflight(self, actor, bot, update):
        with self._actor_transaction(actor) as c:
            c.execute('SELECT bootstrap()')
            old=c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot,update)).fetchone()
            if old:
                return self._voice_cached(c,self._safe_cached(c,old))
            return self._voice_gate(c)

    def receive_voice(self, actor, bot, update, sent_at, data):
        gate=self.voice_preflight(actor,bot,update)
        if gate:
            return gate
        try:
            wav=prepare_voice(data)
        except MediaError as exc:
            return self.receipt_error(actor,bot,update,str(exc))
        with self._actor_transaction(actor) as c:
            old=c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot,update)).fetchone()
            if old:
                return self._voice_cached(c,self._safe_cached(c,old))
            gate=self._voice_gate(c)
            if gate:
                return gate
            import wave
            from io import BytesIO
            import math
            with wave.open(BytesIO(wav),'rb') as audio:duration=max(1,math.ceil(audio.getnframes()/audio.getframerate()))
            row=self._account_context(c)
            job=c.execute('INSERT INTO voice_jobs(workspace_id,author_user_id,account_id,source_sent_at,timezone_snapshot,model,transcribe_model,duration_seconds) VALUES(%s,actor_user_id(),%s,%s,%s,%s,%s,%s) RETURNING *',
                          (row['id'],row['account_id'],sent_at,row['timezone'],self.voice_ai.model,self.voice_ai.transcribe_model,duration)).fetchone()
            categories=[{'id':str(r['id']),'name':r['name']} for r in c.execute('SELECT id,name FROM categories WHERE NOT archived AND workspace_id=%s',(row['id'],)).fetchall()]
            reply=Reply('Распознаю голос…',voice_job_id=str(job['id']))
            c.execute('INSERT INTO telegram_updates(bot_id,update_id,user_id,response) VALUES(%s,%s,actor_user_id(),%s)',(bot,update,Jsonb(asdict(reply))))
        transcript=None;result=None;error=None
        try:
            transcript=mask(self.voice_ai.transcribe(wav)).strip()
            if not transcript or len(transcript)>6000:
                raise ValueError('Transcript limit')
            with self._actor_transaction(actor) as c:
                if not self._voice_job_live(c,job['id']):
                    return Reply('Обработка голоса отменена или истекла.')
                c.execute('UPDATE voice_jobs SET transcript=%s WHERE id=%s',(transcript,job['id']))
            today=sent_at.astimezone(ZoneInfo(row['timezone'])).date().isoformat()
            result=validate_result(self.voice_ai.extract(transcript,categories,today),categories,today)
        except Exception as exc:
            error=type(exc).__name__
            log.warning('Ошибка обработки голоса (%s)',error)
        with self._actor_transaction(actor) as c:
            if not self._voice_job_live(c,job['id']):
                return Reply('Обработка голоса отменена или истекла.')
            if error:
                reply=Reply('Не удалось распознать или разобрать голос. Ничего не сохранено. Отправьте запись заново или /manual.' + ('\nРасшифровка: '+transcript[:1500] if transcript else ''))
            elif result.kind!='expense' or result.currency!=row['currency']:
                reply=Reply('Нужен один совершённый расход в валюте выбранного счёта. Доходы, переводы, планы и несколько расходов в одной записи пока не поддерживаются. Ничего не сохранено. /manual — ввод вручную.\nРасшифровка: '+transcript[:1500])
            elif self._draft(c):
                reply=Reply('Уже открыт другой черновик. Голос не сохранён. Завершите его и отправьте запись заново.')
            else:
                rules=c.execute('SELECT * FROM category_rules WHERE workspace_id=%s AND enabled',(row['id'],)).fetchall()
                category=match_rule(result.description,rules) or result.category_id
                c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,timezone_snapshot,source_sent_at,amount,description,occurred_on,category_id,step,flow,voice_job_id,category_source) VALUES(%s,actor_user_id(),%s,%s,%s,%s,%s,%s,%s,'confirm','auto',%s,%s)",
                          (row['id'],row['account_id'],row['timezone'],sent_at,result.amount,result.description,result.occurred_on,UUID(category) if category else None,job['id'],'rule' if category and category!=result.category_id else 'ai'))
                # Persist date explanation before rendering the preview.
                c.execute('UPDATE voice_jobs SET result=%s WHERE id=%s',(Jsonb(result.model_dump()),job['id']))
                reply=self._voice_prompt(c,self._draft(c))
            c.execute('UPDATE voice_jobs SET state=%s,reply=%s,error_code=%s,result=%s WHERE id=%s',('failed' if error else 'ready',Jsonb(asdict(reply)),error,Jsonb(result.model_dump()) if result else None,job['id']))
            return reply

    def _voice_job_live(self,c,job_id):
        return c.execute("SELECT id FROM voice_jobs WHERE id=%s AND state='processing' AND lease_until>now() AND (SELECT voice_enabled FROM user_settings WHERE user_id=actor_user_id())",(job_id,)).fetchone() is not None

    def _voice_command(self,c,user_id,command,arg):
        if command=='/cancel' or (command=='/voice' and arg=='off'):
            c.execute("UPDATE voice_jobs SET state='cancelled' WHERE state='processing'")
        if command!='/voice':
            return None
        if arg=='on':
            return self._voice_callback(c,user_id,'voice_on')
        if arg=='off':
            c.execute('UPDATE user_settings SET voice_enabled=false WHERE user_id=%s',(user_id,))
            c.execute("UPDATE operation_drafts SET state='cancelled' WHERE voice_job_id IS NOT NULL AND state='pending'")
            return Reply('Обработка голоса выключена. /manual — ручной ввод.')
        if not c.execute('SELECT voice_enabled FROM user_settings WHERE user_id=%s',(user_id,)).fetchone()['voice_enabled']:
            return self._voice_disabled()
        d=self._draft(c)
        if d and d['voice_job_id']:
            return self._voice_prompt(c,d)
        job=c.execute('SELECT id FROM voice_jobs ORDER BY created_at DESC LIMIT 1').fetchone()
        if job:
            reply=self._voice_cached(c,Reply('',voice_job_id=str(job['id'])))
            reply.text+='\nМожно отправить новый голос после завершения текущего черновика.'
            return reply
        return Reply('Отправьте голос: «Вчера потратил восемьсот пятьдесят рублей на продукты». До 3 минут и 15 МБ, один расход. Перед сохранением проверьте карточку.')

    def _voice_callback(self,c,user_id,callback):
        if callback=='voice_on':
            c.execute("UPDATE user_settings SET voice_enabled=true,voice_consented_at=now(),voice_consent_version='voice-v1' WHERE user_id=%s",(user_id,))
            return Reply('Голосовой ввод включён. Отправьте голосовое сообщение об одном расходе в рублях. Например: «Вчера потратил 850 рублей на продукты».')
        if not callback.startswith(('vfield:','vduplicate:')):
            return None
        parts=callback.split(':')
        try:
            d=c.execute("SELECT * FROM operation_drafts WHERE id=%s AND state='pending' AND voice_job_id IS NOT NULL AND version=%s",(UUID(parts[1]),int(parts[2]))).fetchone()
        except (ValueError,IndexError):
            d=None
        if not d:
            return Reply('Карточка устарела. /voice — текущий черновик.')
        if parts[0]=='vduplicate':
            c.execute('UPDATE operation_drafts SET duplicate_confirmed=true,version=version+1 WHERE id=%s',(d['id'],))
        elif len(parts)==4 and parts[3] in ('amount','date','description'):
            c.execute('UPDATE operation_drafts SET voice_edit_field=%s,version=version+1 WHERE id=%s',(parts[3],d['id']))
        return self._voice_prompt(c,self._draft(c))

    def _voice_prompt(self,c,d):
        from balans.domain import CURRENCY
        CURRENCY.set(self._account_currency(c,d['account_id']))
        if d['step']=='category':
            return self._category_menu(c,d)
        if d['voice_edit_field']:
            return Reply({'amount':'Введите сумму в валюте счёта, например 850,50.', 'date':'Введите дату: сегодня, вчера или ДД.ММ.ГГГГ.', 'description':'Введите описание покупки, до 500 символов.'}[d['voice_edit_field']]+'\n/cancel — отменить расход.')
        job=c.execute('SELECT transcript,result FROM voice_jobs WHERE id=%s',(d['voice_job_id'],)).fetchone()
        category=c.execute('SELECT name FROM categories WHERE id=%s',(d['category_id'],)).fetchone() if d['category_id'] else None
        ready=d['amount'] is not None and d['occurred_on'] is not None and category and bool(d['description'])
        duplicates=ready and self._receipt_duplicates(c,d) and not d['duplicate_confirmed']
        buttons=[]
        if ready and not duplicates:
            buttons.append([('Сохранить',f"save:{d['id']}:{d['version']}")])
        if duplicates:
            buttons.append([('Это другая покупка',f"vduplicate:{d['id']}:{d['version']}")])
        buttons += [[(label,f"vfield:{d['id']}:{d['version']}:{field}") for label,field in [('Сумма','amount'),('Дата','date'),('Описание','description')]],
                    [('Категория',f"recat:{d['id']}:{d['version']}"),('Отмена',f"cancel:{d['id']}:{d['version']}")]]
        return Reply(f"Описание: {d['description'] or 'уточните'}\n"
                     f"Сумма: {money(d['amount']) if d['amount'] is not None else 'уточните'}\nКатегория: {category['name'] if category else 'выберите'}\n"
                     f"Дата: {d['occurred_on'].strftime('%d.%m.%Y') if d['occurred_on'] else 'уточните'}"
                     + ('\nПохожий расход уже существует. Подтвердите, что это другая покупка.' if duplicates else ''),buttons)

    def _voice_text(self,c,d,text):
        field=d['voice_edit_field']
        if not field:
            return self._voice_prompt(c,d) if d['step']!='category' else None
        if field=='amount':
            c.execute('UPDATE operation_drafts SET amount=%s WHERE id=%s',(amount_from_text(text),d['id']))
        elif field=='date':
            c.execute('UPDATE operation_drafts SET occurred_on=%s WHERE id=%s',(date_from_text(text,d['source_sent_at'],d['timezone_snapshot']),d['id']))
            c.execute("UPDATE voice_jobs SET result=jsonb_set(result,'{date_note}',%s) WHERE id=%s",(Jsonb('Дата исправлена вручную'),d['voice_job_id']))
        else:
            if not text or len(text)>500:
                raise ValueError('Описание должно содержать от 1 до 500 символов.')
            c.execute('UPDATE operation_drafts SET description=%s WHERE id=%s',(mask(text),d['id']))
        c.execute("UPDATE operation_drafts SET voice_edit_field=NULL,step='confirm',version=version+1,duplicate_confirmed=false WHERE id=%s",(d['id'],))
        return self._voice_prompt(c,self._draft(c))
