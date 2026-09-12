"""Durable opt-in outbox. Uncertain Telegram deliveries are not retried blindly."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID
from decimal import Decimal
from zoneinfo import ZoneInfo
from balans.domain import Reply, money
from balans.planning import next_allowed, latest_slot, monthly_slot


class Notifications:
    def _enqueue_notice(self,c,p,kind,text,key,now,entity_kind=None,entity=None):
        c.execute('INSERT INTO notification_outbox(workspace_id,user_id,telegram_user_id,kind,message,event_key,next_attempt_at,entity_kind,entity_id,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(p['workspace_id'],p['user_id'],p['telegram_user_id'],kind,text,key,now,entity_kind,entity,now+timedelta(days=7)))

    def plan_notifications(self,now=None):
        now=now or datetime.now(timezone.utc)
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.notification_worker','on',true)")
            jobs=c.execute('SELECT id,telegram_user_id,workspace_id FROM balans.notification_preferences WHERE enabled AND NOT blocked AND next_check_at<=%s ORDER BY next_check_at FOR UPDATE SKIP LOCKED LIMIT 100',(now,)).fetchall()
            for job in jobs:c.execute('UPDATE balans.notification_preferences SET next_check_at=%s WHERE id=%s',(now+timedelta(minutes=1),job['id']))
        for job in jobs:
            with self._actor_transaction(job['telegram_user_id']) as c:
                workspace=c.execute('SELECT id,name FROM workspaces WHERE id=%s',(job['workspace_id'],)).fetchone()
                if not workspace:
                    c.execute('UPDATE notification_preferences SET enabled=false WHERE id=%s',(job['id'],))
                    c.execute("UPDATE notification_outbox SET state='cancelled' WHERE workspace_id=%s AND state='pending'",(job['workspace_id'],));continue
                c.execute("SELECT set_config('balans.workspace_id',%s,true)",(str(workspace['id']),))
                p=c.execute('SELECT * FROM notification_preferences WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
                if not p or not p['enabled'] or p['blocked']:continue
                zone=c.execute('SELECT timezone FROM user_settings WHERE user_id=actor_user_id()').fetchone()['timezone']
                prefix='Бюджет «'+workspace['name']+'». '
                access=c.execute('SELECT billing_access() AS data').fetchone()['data']
                if access['status'] in ('active','trial','expired') and access.get('until') and access['sponsor']==str(p['user_id']):
                    expiry=datetime.fromisoformat(access['until'])
                    if expiry<=now+timedelta(days=1):
                        status='ended' if expiry<=now else 'ending'
                        message='Доступ завершён. История и экспорт сохранены. /subscription — оплатить.' if status=='ended' else 'Доступ заканчивается в течение суток. Если Stars для продления недостаточно, новые операции будут недоступны. /subscription — проверить подписку.'
                        if access['status']=='trial' and status=='ending':message='Бесплатный период заканчивается в течение суток. Оплата не подключится автоматически. /subscription — тариф и условия.'
                        self._enqueue_notice(c,p,'billing',message,f'billing:{expiry.isoformat()}:{status}',now,'billing')
                if p['budget_alerts']:
                    start,end,rows=self._budget_rows(c,now)
                    for b in rows:
                        threshold=100 if b['spent']>=b['amount'] else 80 if b['spent']*100>=b['amount']*80 else None
                        if not threshold:continue
                        fresh=c.execute('INSERT INTO budget_alerts(workspace_id,author_user_id,budget_id,period_start,threshold) VALUES(current_workspace(),actor_user_id(),%s,%s,%s) ON CONFLICT DO NOTHING RETURNING threshold',(b['id'],start,threshold)).fetchone()
                        if threshold==100:c.execute('INSERT INTO budget_alerts(workspace_id,author_user_id,budget_id,period_start,threshold) VALUES(current_workspace(),actor_user_id(),%s,%s,80) ON CONFLICT DO NOTHING',(b['id'],start))
                        if fresh:self._enqueue_notice(c,p,'budget',prefix+f"Лимит «{b['name']}»: достигнуто {threshold}%. Потрачено {money(b['spent'],b['currency'])} из {money(b['amount'],b['currency'])}. Период до {end:%d.%m.%Y}.",f"budget:{b['id']}:{start}:{threshold}",now,'budget',b['id'])
                for kind in ('reminder','weekly'):
                    if not p[kind]:continue
                    slot=latest_slot(now,zone,p['send_minute'],p['weekday'] if kind=='weekly' else None)
                    if slot<p['enabled_at']:continue
                    key=f"{kind}:{p['workspace_id']}:{slot.isoformat()}"
                    if c.execute('SELECT id FROM notification_outbox WHERE event_key=%s',(key,)).fetchone():continue
                    text=prefix+'Проверьте, все ли расходы внесены за день.';entity_kind='budget';entity=None
                    if kind=='weekly':
                        end=slot.astimezone(ZoneInfo(zone)).date()-timedelta(days=1);start=end-timedelta(days=6)
                        try:
                            report=self._report_snapshot(c,p['user_id'],f'{start} {end}',now)
                            text=prefix+f"Неделя {start:%d.%m}–{end:%d.%m}: расходы {money(Decimal(report['snapshot']['summary']['total']),report['snapshot'].get('currency','RUB'))}. Откройте отчёт для подробностей."
                            entity_kind='report';entity=report['id']
                        except ValueError:text=prefix+'Недельный отчёт: откройте /report и выберите период или участника.'
                    self._enqueue_notice(c,p,kind,text,key,now,entity_kind,entity)
                self._plan_monthly(c,p,zone,prefix,now)
                if p['shared_mode']=='instant':
                    events=c.execute('SELECT * FROM notification_events WHERE workspace_id=current_workspace() AND NOT processed ORDER BY created_at LIMIT 100 FOR UPDATE').fetchall()
                    for e in events:
                        if e['event_type'] in p['event_types']:
                            self._enqueue_notice(c,p,'event',prefix+'Изменения в учёте. Откройте запись, чтобы проверить подробности.',e['event_key'],now,e['entity_kind'],e['entity_id'])
                        c.execute('UPDATE notification_events SET processed=true WHERE id=%s',(e['id'],))
                elif p['shared_mode']=='daily':
                    slot=latest_slot(now,zone,p['send_minute']);key=f"digest:{workspace['id']}:{slot.isoformat()}"
                    if slot>=p['enabled_at'] and not c.execute('SELECT id FROM notification_outbox WHERE event_key=%s',(key,)).fetchone():
                        total=c.execute('WITH done AS (UPDATE notification_events SET processed=true WHERE workspace_id=current_workspace() AND NOT processed AND created_at<=%s AND event_type=ANY(%s) RETURNING id) SELECT count(*) AS n FROM done',(slot,p['event_types'])).fetchone()['n']
                        if total:self._enqueue_notice(c,p,'digest',prefix+f'Новых событий: {total}. Откройте учёт для проверки.',key,now,'reviewqueue')

    def _plan_monthly(self,c,p,zone,prefix,now):
        if not p['monthly']:return
        slot=monthly_slot(now,zone,p['send_minute'])
        if slot<max(p['enabled_at'],p['monthly_enabled_at'] or p['enabled_at']):return
        end=slot.astimezone(ZoneInfo(zone)).date()-timedelta(days=1)
        start=end.replace(day=1)
        key=f"monthly:{p['workspace_id']}:{start.isoformat()}"
        if c.execute('SELECT id FROM notification_outbox WHERE event_key=%s',(key,)).fetchone():return
        try:
            report=self._report_snapshot(c,p['user_id'],f'{start} {end} | currency=all',now)
            snapshots=report['snapshot'].get('currency_reports',[report['snapshot']])
            lines=[prefix+f'Итоги месяца {start:%m.%Y}']
            for snapshot in snapshots:
                summary=snapshot['summary'];currency=snapshot.get('currency','RUB')
                lines.append(f"\n{currency}: расходы {money(Decimal(summary['total']),currency)}, доходы {money(Decimal(summary.get('income','0')),currency)}.")
                for category in summary.get('categories',[])[:3]:
                    lines.append(f"• {category['name'][:60]}: {money(Decimal(category['total']),currency)}")
                previous=Decimal(snapshot['previous']['total'])
                if previous:
                    lines.append('Изменение расходов к прошлому месяцу: '+money(Decimal(snapshot['delta']),currency)+'.')
                else:lines.append('Нет записанных расходов прошлого месяца для сравнения.')
            lines.append('\nПодробности — в отчёте. /notify monthly off — отключить.')
            self._enqueue_notice(c,p,'monthly','\n'.join(lines),key,now,'report',report['id'])
        except ValueError:
            self._enqueue_notice(c,p,'monthly',prefix+f'Итоги месяца {start:%m.%Y}: откройте /report {start:%Y-%m} и выберите более короткий период для подробностей.',key,now,'report')

    def _monthly_settings(self,c):
        p=self._preference(c)
        enabled=p['monthly'] and p['enabled']
        return Reply('Ежемесячный отчёт: '+('включён' if enabled else 'выключен')+'.\n'
                     'Приходит 1-го числа за предыдущий месяц: расходы, доходы, основные категории и сравнение. '
                     'Время и тихие часы: /notify. Обычный отчёт не расходует квоту ИИ.',
                     [[('Отключить' if enabled else 'Включить ежемесячный отчёт','monthlyoff' if enabled else 'monthlyon')],[('Назад','start')]])

    def claim_notifications(self,now=None):
        now=now or datetime.now(timezone.utc)
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.notification_worker','on',true)")
            c.execute("UPDATE balans.notification_outbox SET state='uncertain' WHERE state='sending' AND lease_until<%s",(now,))
            c.execute("UPDATE balans.notification_outbox SET state='cancelled' WHERE state='pending' AND expires_at<=%s",(now,))
            return c.execute("WITH due AS (SELECT id FROM balans.notification_outbox WHERE state='pending' AND next_attempt_at<=%s ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 20) UPDATE balans.notification_outbox n SET state='sending',lease_until=%s,attempts=attempts+1 FROM due WHERE n.id=due.id RETURNING n.id,n.telegram_user_id",(now,now+timedelta(minutes=2))).fetchall()

    def prepare_notification(self,actor,identity,now=None):
        now=now or datetime.now(timezone.utc)
        with self._actor_transaction(actor) as c:
            n=c.execute("SELECT * FROM notification_outbox WHERE id=%s AND state='sending' FOR UPDATE",(identity,)).fetchone()
            if not n:return None
            p=c.execute('SELECT * FROM notification_preferences WHERE workspace_id=%s',(n['workspace_id'],)).fetchone()
            accessible=c.execute('SELECT id FROM workspaces WHERE id=%s',(n['workspace_id'],)).fetchone()
            permitted=p and p['enabled'] and not p['blocked'] and accessible and n['expires_at']>now
            if permitted:
                permitted= True if n['kind']=='billing' else p['budget_alerts'] if n['kind']=='budget' else p[n['kind']] if n['kind'] in ('reminder','weekly','monthly') else p['shared_mode']!='off'
            if not permitted:
                c.execute("UPDATE notification_outbox SET state='cancelled' WHERE id=%s",(identity,));return None
            zone=c.execute('SELECT timezone FROM user_settings WHERE user_id=actor_user_id()').fetchone()['timezone']
            allowed=next_allowed(now,p['quiet_start'],p['quiet_end'],zone)
            if allowed>now:
                c.execute("UPDATE notification_outbox SET state='pending',next_attempt_at=%s WHERE id=%s",(allowed,identity));return None
            return n

    def finish_notification(self,actor,identity,state,retry_at=None):
        with self._actor_transaction(actor) as c:
            c.execute("UPDATE notification_outbox SET state=%s,next_attempt_at=coalesce(%s,next_attempt_at) WHERE id=%s AND state='sending'",(state,retry_at,identity))

    def notifications_blocked(self,actor,blocked=True):
        with self._actor_transaction(actor) as c:c.execute('UPDATE notification_preferences SET blocked=%s WHERE user_id=actor_user_id()',(blocked,))

    def _notification_callback(self,c,user,callback,sent):
        if callback in ('monthlysettings','monthlyon','monthlyoff'):
            p=self._preference(c)
            if callback=='monthlyon':
                c.execute('UPDATE notification_preferences SET monthly_enabled_at=CASE WHEN monthly AND enabled THEN monthly_enabled_at ELSE now() END,monthly=true,enabled=true,enabled_at=CASE WHEN enabled THEN enabled_at ELSE now() END,next_check_at=now() WHERE id=%s',(p['id'],))
            elif callback=='monthlyoff':
                c.execute('UPDATE notification_preferences SET monthly=false WHERE id=%s',(p['id'],))
                c.execute("UPDATE notification_outbox SET state='cancelled' WHERE workspace_id=current_workspace() AND kind='monthly' AND state='pending'")
            return self._monthly_settings(c)
        if not callback.startswith('nopen:'):return None
        n=c.execute('SELECT * FROM notification_outbox WHERE id=%s',(UUID(callback[6:]),)).fetchone()
        if not n or not c.execute('SELECT id FROM workspaces WHERE id=%s',(n['workspace_id'],)).fetchone():return Reply('Доступ к уведомлению прекращён.')
        current=c.execute('SELECT current_workspace() AS id').fetchone()['id']
        if current!=n['workspace_id']:
            if self._workspace_busy(c):return Reply('Сначала завершите текущий ввод; затем откройте уведомление снова.')
            c.execute('UPDATE user_settings SET selected_workspace_id=%s,default_account_id=NULL WHERE user_id=%s',(n['workspace_id'],user))
        kind=n['entity_kind']
        if kind=='billing':return self._subscription_card(c)
        if kind in ('operation','transfer','claim'):return self._documents_command(c,user,'/docs',f"{kind} {n['entity_id']}",sent)
        if kind=='report':
            report=self._get_report(c,n['entity_id'])
            return self._report_card(report) if report else Reply('Отчёт истёк. /report — создать новый.')
        if kind=='reviewqueue':return self._documents_command(c,user,'/reviewqueue','',sent)
        return self._budget_card(c,sent)


async def notification_loop(bot,service):
    from aiogram.exceptions import TelegramForbiddenError,TelegramRetryAfter
    from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
    log=logging.getLogger('balans.notifications')
    while True:
        try:
            await asyncio.to_thread(service.plan_notifications)
            for job in await asyncio.to_thread(service.claim_notifications):
                actor=job['telegram_user_id'];identity=job['id']
                n=await asyncio.to_thread(service.prepare_notification,actor,identity)
                if not n:continue
                try:
                    await bot.send_message(actor,n['message'],reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть',callback_data=f'nopen:{identity}')]]))
                except TelegramForbiddenError:
                    await asyncio.to_thread(service.notifications_blocked,actor)
                    await asyncio.to_thread(service.finish_notification,actor,identity,'cancelled')
                except TelegramRetryAfter as exc:
                    await asyncio.to_thread(service.finish_notification,actor,identity,'pending',datetime.now(timezone.utc)+timedelta(seconds=exc.retry_after))
                except Exception:
                    await asyncio.to_thread(service.finish_notification,actor,identity,'uncertain')
                else:await asyncio.to_thread(service.finish_notification,actor,identity,'sent')
        except Exception as exc:log.error('Ошибка очереди уведомлений (%s)',type(exc).__name__)
        await asyncio.sleep(10)
