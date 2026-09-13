"""Explicit consent, private retention choices and durable account erasure."""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID
import hashlib,hmac,json,os,secrets
from balans.domain import Reply

class Privacy:
    def _privacy_key(self):
        if hasattr(self,'_privacy_key_value'):return self._privacy_key_value
        import fcntl,stat
        path=self.receipt_storage.root.parent/'privacy.key'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise RuntimeError('Ключ приватности должен быть обычным файлом с правами 600.')
            key=f.read()
            if not key:
                key=secrets.token_bytes(32);f.write(key);f.flush();os.fsync(f.fileno())
            if len(key)!=32:raise RuntimeError('Некорректный ключ приватности.')
        self._privacy_key_value=key
        return key

    def _actor_hash(self,actor):return hmac.new(self._privacy_key(),str(actor).encode(),hashlib.sha256).hexdigest()

    @contextmanager
    def _actor_transaction(self,telegram_id):
        with super()._actor_transaction(telegram_id) as c:
            c.execute("SELECT set_config('balans.actor_hash',%s,true)",(self._actor_hash(telegram_id),))
            from balans.domain import CURRENCY
            token=CURRENCY.set('RUB')
            self._account_context(c)
            try:yield c
            finally:CURRENCY.reset(token)

    def _privacy_blocked(self,c):
        row=c.execute("SELECT state FROM erasure_requests WHERE telegram_hash=current_setting('balans.actor_hash') AND state<>'preview'").fetchone()
        if row:return Reply('Личные данные удалены. Общая история других участников и минимальные платёжные записи хранятся отдельно.' if row['state']=='completed' else 'Удаление подтверждено. Доступ к данным закрыт; активные личные данные и файлы будут удалены в течение 7 дней.')
        return None

    def _log_erasure(self,request):
        payload={'id':str(request['id']),'user_id':str(request['user_id']),'telegram_hash':request['telegram_hash'],'confirmed_at':str(request['confirmed_at'])}
        raw=json.dumps(payload,sort_keys=True,separators=(',',':'));record={'data':payload,'signature':hmac.new(self._privacy_key(),raw.encode(),hashlib.sha256).hexdigest()}
        path=self.receipt_storage.root.parent/'deletions.jsonl'
        if path.is_symlink():raise RuntimeError('Реестр удалений не должен быть ссылкой.')
        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a') as f:f.write(json.dumps(record)+'\n');f.flush();os.fsync(f.fileno())

    def handle(self,telegram_id,bot_id,update_id,text,sent_at,callback=None):
        with self._actor_transaction(telegram_id) as c:
            blocked=self._privacy_blocked(c)
            if blocked:return blocked
            if callback and callback.startswith('eraseconfirm:'):
                identity=UUID(callback.split(':')[1])
                from psycopg.errors import RaiseException
                try:
                    with c.transaction():c.execute('SELECT confirm_erasure(%s)',(identity,))
                except RaiseException as exc:return Reply(exc.diag.message_primary)
                request=c.execute('SELECT * FROM erasure_requests WHERE id=%s',(identity,)).fetchone()
                self._log_erasure(request)
                return Reply('Удаление подтверждено. Доступ закрыт немедленно; личные данные и файлы удаляются в течение 7 дней. Общая история других участников сохраняется, ваши общие бюджеты архивированы.')
        return self._handle_impl(telegram_id,bot_id,update_id,text,sent_at,callback)

    def _privacy_consent_gate(self,c):
        if c.execute('SELECT required FROM privacy_policy').fetchone()['required'] and not c.execute('SELECT 1 FROM user_settings WHERE user_id=actor_user_id() AND service_consent_version=(SELECT version FROM privacy_policy)').fetchone():return Reply('Сначала ознакомьтесь с условиями: /privacy')
        return None

    def _voice_gate(self,c):return self._privacy_consent_gate(c) or super()._voice_gate(c)
    def _receipt_gate(self,c):return self._privacy_consent_gate(c) or super()._receipt_gate(c)

    def voice_preflight(self,actor,bot,update,seconds=1):
        with self._actor_transaction(actor) as c:
            if blocked:=self._privacy_blocked(c):return blocked
        return super().voice_preflight(actor,bot,update,seconds)

    def receipt_preflight(self,actor,bot,update):
        with self._actor_transaction(actor) as c:
            if blocked:=self._privacy_blocked(c):return blocked
        return super().receipt_preflight(actor,bot,update)

    def _privacy_command(self,c,user,command,arg,sent):
        if command=='/delete':
            row=c.execute("INSERT INTO erasure_requests(user_id,telegram_hash,telegram_user_id) VALUES(%s,current_setting('balans.actor_hash'),actor_telegram_id()) ON CONFLICT(telegram_hash) DO UPDATE SET expires_at=now()+interval '1 hour' RETURNING id",(user,)).fetchone()
            return Reply('Удалить личный профиль?\nПосле подтверждения доступ сразу закроется. Личные операции, настройки и личные файлы удаляются в течение 7 дней; резервные копии должны истечь не позже 30 дней.\nОбщие операции и документы других участников сохраняются; общие бюджеты, которыми вы руководите, станут архивными. Минимальные сведения об оплате хранятся отдельно по опубликованной политике.\nСообщения и пересланные копии в Telegram бот удалить не может. Перед удалением сохраните нужное через /csv. Действие необратимо.',[[('Подтверждаю удаление личного профиля',f"eraseconfirm:{row['id']}")]])
        if command=='/retention':
            if arg not in ('','on','off'):raise ValueError('/retention on или /retention off — хранение личных оригиналов после обработки.')
            if arg:c.execute('UPDATE user_settings SET keep_personal_originals=%s WHERE user_id=%s',(arg=='on',user))
            row=c.execute('SELECT keep_personal_originals FROM user_settings WHERE user_id=%s',(user,)).fetchone()
            return Reply('Хранение личных оригиналов после обработки: '+('до 90 дней' if row['keep_personal_originals'] else 'выключено')+'. При выключении существующие личные оригиналы удалятся при ближайшей очистке. Для общих документов действует срок 90 дней. Перед удалением предложим платное продление.\n/retention on или off')
        if command=='/privacy':
            p=c.execute('SELECT * FROM privacy_policy').fetchone()
            return Reply('Приватность\nЛичные операции доступны вам; в общем бюджете руководитель видит общую историю по правилам /join. AI вызывается после отдельного согласия для текста, голоса и изображений.\nГолосовой исходник обрабатывается в памяти и не сохраняется. Личные изображения — до 90 дней; /retention off отключает хранение после обработки. Общие оригиналы — 90 дней. Перед удалением предложим платное продление на 90 дней.\n/delete — удаление личного профиля. Платёжные записи: '+p['payment_retention']+f"\nУсловия: {p['terms_url'] or 'ещё не опубликованы'}\nПолитика: {p['privacy_url'] or 'ещё не опубликована'}\nСтрана оператора: {p['operator_country'] or 'не настроена'}; размещение: {p['storage_country'] or 'не настроено'}",[[('Принимаю условия и политику',f"privacyaccept:{p['version']}")]] if p['terms_url'] and p['privacy_url'] else [])
        return None

    def _privacy_callback(self,c,user,callback):
        if not callback.startswith('privacyaccept:'):return None
        p=c.execute('SELECT * FROM privacy_policy').fetchone()
        if int(callback.split(':')[1])!=p['version'] or not p['terms_url'] or not p['privacy_url']:return Reply('Условия изменились или не опубликованы. /privacy')
        c.execute('UPDATE user_settings SET service_consent_version=%s,service_consented_at=now() WHERE user_id=%s',(p['version'],user));return Reply('Согласие сохранено. Разрешения на передачу данных AI включаются отдельно: /ai, /receipts, /voice.')

    def process_erasures(self):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.erasure_worker','on',true)")
            requests=c.execute("SELECT id FROM balans.erasure_requests WHERE state IN ('queued','files_pending') ORDER BY confirmed_at LIMIT 10").fetchall()
        for r in requests:
            with self.pool.connection() as c,c.transaction():files=c.execute('SELECT balans.erase_account(%s) AS files',(r['id'],)).fetchone()['files']
            for file in files:
                storage=self.receipt_storage if file['kind']=='receipt' else self.document_storage.shared if file['kind']=='shared' else self.document_storage.personal
                storage.remove(UUID(file['id']))
            with self.pool.connection() as c,c.transaction():
                c.execute("SELECT set_config('balans.erasure_worker','on',true)")
                c.execute("UPDATE balans.erasure_requests SET state='completed',completed_at=now(),files='[]' WHERE id=%s AND state='files_pending'",(r['id'],))

    def purge_private_files(self):
        with self.pool.connection() as c,c.transaction():
            c.execute('SELECT balans.schedule_retention()')
            c.execute("SELECT set_config('balans.erasure_worker','on',true)")
            rows=c.execute("SELECT id,kind FROM balans.file_purge_queue WHERE state='pending' LIMIT 1000").fetchall()
        for row in rows:
            storage=self.receipt_storage if row['kind']=='receipt' else self.document_storage.shared if row['kind']=='shared' else self.document_storage.personal
            storage.remove(row['id'])
            with self.pool.connection() as c,c.transaction():
                c.execute("SELECT set_config('balans.erasure_worker','on',true)")
                c.execute("UPDATE balans.file_purge_queue SET state='done' WHERE id=%s AND kind=%s",(row['id'],row['kind']))


async def privacy_loop(service):
    import asyncio,logging
    log=logging.getLogger('balans.privacy');rounds=0
    while True:
        try:
            await asyncio.to_thread(service.process_erasures)
            if rounds%15==0:await asyncio.to_thread(service.purge_private_files)
        except Exception as exc:log.error('Ошибка очистки приватных данных (%s)',type(exc).__name__)
        rounds+=1;await asyncio.sleep(60)
