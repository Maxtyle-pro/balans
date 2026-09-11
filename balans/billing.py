"""Stars subscription ledger; only authenticated Telegram payment updates grant access."""
from datetime import datetime,timedelta,timezone
from uuid import UUID
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb
from balans.domain import Reply

PERIOD=2592000


class Billing:
    def _billing_gate(self,c):
        info=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if info['status'] in ('expired','suspended'):return Reply('Доступ к новым операциям завершён. /subscription — подписка владельца бюджета; история и экспорт доступны.')
        return None

    def _voice_gate(self,c):return self._billing_gate(c) or super()._voice_gate(c)
    def _receipt_gate(self,c):return self._billing_gate(c) or super()._receipt_gate(c)

    def receive_voice(self,*args,**kwargs):
        try:return super().receive_voice(*args,**kwargs)
        except RaiseException as exc:return Reply(exc.diag.message_primary)

    def receive_receipt(self,*args,**kwargs):
        try:return super().receive_receipt(*args,**kwargs)
        except RaiseException as exc:return Reply(exc.diag.message_primary)

    def _subscription_card(self,c):
        cfg=c.execute('SELECT * FROM billing_config').fetchone();access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if not cfg['enabled']:return Reply('Оплата пока не включена. Текущий доступ работает без подписочных ограничений.')
        labels={'active':'Оплачено','trial':'Пробный период','expired':'Доступ истёк','admin_free':'Бесплатный доступ владельца','suspended':'Доступ приостановлен'}
        text=f"Подписка: {labels.get(access['status'],access['status'])}\nДоступ до: {access.get('until') or 'без срока'}\nЦена: {cfg['stars']} Stars за 30 дней, автопродление каждые 30 дней.\nПробный период: {cfg['trial_days']} дней.\nЛичные и общие бюджеты, ручной ввод, отчёты; AI в пределах квот.\nКвоты в календарный месяц UTC: текст {cfg['text_quota']}, голос {cfg['voice_seconds']} секунд, изображения/файлы {cfg['image_quota']}.\nОбщий бюджет оплачивает руководитель; квоты расходуются у него.\nОтмена продления сохраняет уже оплаченный период. Возврат прекращает доступ по возвращённому платежу.\nУсловия и возврат: {cfg['terms_url']}\n/paysupport — вопросы оплаты; /renewal — отмена продления."
        buttons=[]
        if access['status'] not in ('admin_free','suspended'):buttons=[[('Прочитал условия, перейти к оплате',f"billbuy:{cfg['terms_version']}")]]
        return Reply(text,buttons)

    def _billing_command(self,c,user,command,arg,sent):
        if command in ('/subscription','/subscribe'):return self._subscription_card(c)
        if command=='/paysupport':
            cfg=c.execute('SELECT support_contact FROM billing_config').fetchone()
            return Reply((cfg['support_contact'] or self.support or 'Поддержка оплаты пока не настроена.')+'\nВопросы оплаты решает владелец сервиса, не поддержка Telegram.')
        if command=='/terms':return Reply(c.execute('SELECT terms_url FROM billing_config').fetchone()['terms_url'] or 'Условия оплаты ещё не опубликованы; приём платежей выключен.')
        if command=='/renewal':
            rows=c.execute('SELECT id,first_charge_id,auto_renew FROM billing_invoices WHERE user_id=%s AND first_charge_id IS NOT NULL ORDER BY created_at DESC LIMIT 10',(user,)).fetchall()
            return Reply('Продление подписок. Отмена сохраняет оплаченный срок.',[[('Отменить автопродление' if r['auto_renew'] else 'Возобновить автопродление',f"billrenew:{r['id']}:{0 if r['auto_renew'] else 1}")] for r in rows])
        return None

    def _billing_callback(self,c,user,callback,sent):
        if callback.startswith('billbuy:'):
            cfg=c.execute('SELECT * FROM billing_config').fetchone()
            if not cfg['enabled'] or int(callback.split(':')[1])!=cfg['terms_version']:return Reply('Условия изменились или оплата выключена. /subscription')
            # One live subscription per user. Shared participants purchase only their own access.
            existing=c.execute('SELECT id FROM billing_invoices WHERE user_id=%s AND (expires_at>now() AND first_charge_id IS NULL OR auto_renew AND first_charge_id IS NOT NULL) ORDER BY created_at DESC LIMIT 1',(user,)).fetchone()
            if existing:
                paid=c.execute('SELECT first_charge_id FROM billing_invoices WHERE id=%s',(existing['id'],)).fetchone()
                return Reply('Уже есть подписка с автопродлением. /renewal — управление.') if paid['first_charge_id'] else Reply('Счёт на оплату',invoice_id=str(existing['id']))
            row=c.execute('INSERT INTO billing_invoices(user_id,telegram_user_id,stars,terms_version,terms_url,quotas) VALUES(%s,actor_telegram_id(),%s,%s,%s,%s) RETURNING id',(user,cfg['stars'],cfg['terms_version'],cfg['terms_url'],Jsonb({'text':cfg['text_quota'],'voice':cfg['voice_seconds'],'image':cfg['image_quota']}))).fetchone()
            return Reply('Счёт на оплату',invoice_id=str(row['id']))
        if callback.startswith('billrenew:'):
            _,identity,enabled=callback.split(':');identity=UUID(identity)
            if enabled not in ('0','1'):raise ValueError('Недействительная кнопка.')
            invoice=c.execute('SELECT * FROM billing_invoices WHERE id=%s AND first_charge_id IS NOT NULL',(identity,)).fetchone()
            if not invoice:return Reply('Подписка недоступна.')
            return Reply('Изменяю автопродление…',renewal_invoice_id=str(identity),renewal_enabled=enabled=='1')
        return None

    def prepare_invoice(self,actor,identity):
        with self._actor_transaction(actor) as c:
            cfg=c.execute('SELECT enabled FROM billing_config').fetchone()
            row=c.execute('SELECT * FROM billing_invoices WHERE id=%s AND expires_at>now() AND first_charge_id IS NULL',(UUID(identity),)).fetchone()
            return row if cfg['enabled'] else None

    def cache_invoice_url(self,actor,identity,url):
        with self._actor_transaction(actor) as c:c.execute('UPDATE billing_invoices SET invoice_url=%s WHERE id=%s',(url,UUID(identity)))

    def pre_checkout(self,actor,query_id,payload,currency,total):
        try:identity=UUID(payload)
        except ValueError:return False
        with self._actor_transaction(actor) as c:
            cfg=c.execute('SELECT enabled FROM billing_config').fetchone()
            invoice=c.execute('SELECT * FROM billing_invoices WHERE id=%s FOR UPDATE',(identity,)).fetchone()
            if not cfg['enabled'] or not invoice or invoice['telegram_user_id']!=actor or currency!='XTR' or total!=invoice['stars'] or invoice['first_charge_id'] or invoice['expires_at']<=datetime.now(timezone.utc):return False
            if invoice['checkout_until'] and invoice['checkout_until']>datetime.now(timezone.utc) and invoice['checkout_id']!=query_id:return False
            c.execute("UPDATE billing_invoices SET checkout_id=%s,checkout_until=now()+interval '10 minutes' WHERE id=%s",(query_id,identity))
            return True

    def record_payment(self,actor,payment,refunded=False):
        try:
            identity=UUID(payment['invoice_payload']);charge=payment['telegram_payment_charge_id']
            if not charge or len(charge)>512:raise ValueError()
        except (ValueError,KeyError):return Reply('Платёж требует проверки. /paysupport')
        with self._actor_transaction(actor) as c:
            invoice=c.execute('SELECT * FROM billing_invoices WHERE id=%s FOR UPDATE',(identity,)).fetchone()
            if not invoice or invoice['telegram_user_id']!=actor or payment['currency']!='XTR' or payment['total_amount']!=invoice['stars']:return Reply('Параметры платежа не совпали со счётом. /paysupport')
            old=c.execute('SELECT * FROM billing_payments WHERE charge_id=%s',(charge,)).fetchone()
            if old and (old['invoice_id']!=identity or old['stars']!=payment['total_amount']):return Reply('Платёж требует сверки. /paysupport')
            end=None
            if not refunded:
                expiry=payment.get('subscription_expiration_date')
                if isinstance(expiry,datetime):end=expiry
                elif isinstance(expiry,int):
                    try:end=datetime.fromtimestamp(expiry,timezone.utc)
                    except (ValueError,OverflowError,OSError):pass
                if not end or not payment.get('is_recurring'):return Reply('Не получены параметры подписки. Платёж требует сверки. /paysupport')
            c.execute('INSERT INTO billing_payments(charge_id,invoice_id,user_id,telegram_user_id,stars,currency,period_start,period_end,refunded,success_seen) VALUES(%s,%s,%s,%s,%s,\'XTR\',%s,%s,%s,%s) ON CONFLICT(charge_id) DO UPDATE SET refunded=billing_payments.refunded OR excluded.refunded,success_seen=billing_payments.success_seen OR excluded.success_seen,period_start=coalesce(billing_payments.period_start,excluded.period_start),period_end=coalesce(billing_payments.period_end,excluded.period_end)',(charge,identity,invoice['user_id'],actor,invoice['stars'],end-timedelta(seconds=PERIOD) if end else None,end,refunded,not refunded))
            if not refunded:
                c.execute('UPDATE billing_invoices SET first_charge_id=coalesce(first_charge_id,%s) WHERE id=%s',(charge,identity))
            c.execute('INSERT INTO billing_audit(user_id,event_key,action,details) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(invoice['user_id'],('refund:' if refunded else 'paid:')+charge,'refunded' if refunded else 'paid',Jsonb({'invoice':str(identity),'stars':invoice['stars']})))
            row=c.execute('SELECT refunded FROM billing_payments WHERE charge_id=%s',(charge,)).fetchone()
            return Reply('Возврат Stars учтён. Доступ по этому платежу прекращён; /subscription — статус.' if row['refunded'] else f'Оплата подтверждена: {invoice["stars"]} Stars. Доступ по платежу до {end:%d.%m.%Y %H:%M} UTC. /subscription')

    def renewal_details(self,actor,identity):
        with self._actor_transaction(actor) as c:return c.execute('SELECT * FROM billing_invoices WHERE id=%s AND first_charge_id IS NOT NULL',(UUID(identity),)).fetchone()

    def record_renewal(self,actor,identity,enabled):
        with self._actor_transaction(actor) as c:
            row=c.execute('UPDATE billing_invoices SET auto_renew=%s WHERE id=%s RETURNING user_id',(enabled,UUID(identity))).fetchone()
            if row:c.execute('INSERT INTO billing_audit(user_id,event_key,action) VALUES(%s,%s,%s)',(row['user_id'],f'renew:{identity}:{datetime.now(timezone.utc).isoformat()}','renewal_enabled' if enabled else 'renewal_cancelled'))

    def reconcile_payment(self,actor,payment,refunded):
        # Stars history lacks subscription_expiration_date: never invent an entitlement end.
        if refunded:return self.record_payment(actor,payment,True)
        try:identity=UUID(payment['invoice_payload'])
        except ValueError:return Reply('Требует сверки.')
        with self._actor_transaction(actor) as c:
            invoice=c.execute('SELECT * FROM billing_invoices WHERE id=%s',(identity,)).fetchone()
            if not invoice or invoice['stars']!=payment['total_amount']:return Reply('Требует сверки.')
            existing=c.execute('SELECT * FROM billing_payments WHERE charge_id=%s',(payment['telegram_payment_charge_id'],)).fetchone()
            if existing and existing['success_seen']:return Reply('Оплата подтверждена.')
            c.execute('INSERT INTO billing_audit(user_id,event_key,action,details) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(invoice['user_id'],'reconcile:'+payment['telegram_payment_charge_id'],'reconciliation_missing_expiry',Jsonb({'invoice':str(identity),'charge':payment['telegram_payment_charge_id'],'stars':payment['total_amount']})))
            return Reply('Требует сверки срока подписки: история Stars не содержит точную дату окончания.')
