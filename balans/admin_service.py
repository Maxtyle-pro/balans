"""Administrative metadata and explicit support snapshots, separate from financial RLS."""
from datetime import datetime,timedelta,timezone
from uuid import UUID
from psycopg.types.json import Jsonb
from balans.domain import Reply

class AdminService:
    def _admin_command(self,c,user,command,arg,sent):
        if command=='/admin':
            code=self._create_admin_code(c,c.execute('SELECT actor_telegram_id() AS id').fetchone()['id'])
            return Reply(f'Панель: {self.admin_config.base_url}/login\nОдноразовый код (2 минуты):\n{code}\nВведите его в панели вместе с кодом вашего приложения TOTP. Не пересылайте код.')
        if command=='/support':
            if arg:
                if len(arg)>3000:raise ValueError('Сообщение поддержке — до 3000 символов.')
                row=c.execute('INSERT INTO support_tickets(user_id,telegram_user_id,body) VALUES(%s,actor_telegram_id(),%s) RETURNING id',(user,arg)).fetchone()
                return Reply(f"Обращение сохранено: {row['id']}. Владелец сервиса увидит только присланный вами текст. /support — ответы.")
            tickets=c.execute('SELECT id,state,reply FROM support_tickets WHERE user_id=%s ORDER BY created_at DESC LIMIT 5',(user,)).fetchall()
            intro=c.execute("SELECT value FROM service_content WHERE key='support_intro'").fetchone()
            return Reply(((intro['value'] if intro else self.support) or 'Поддержка: /support Текст обращения')+'\nНе отправляйте токены и пароли. Операции не прикладываются автоматически.\n'+'\n'.join(f"{t['id']} · {t['state']}\n{(t['reply'] or 'Ответа пока нет.')[:500]}" for t in tickets))
        if command=='/diagnostic':
            parts=arg.split()
            if len(parts)==2 and parts[0]=='revoke':
                c.execute('UPDATE diagnostic_grants SET revoked=true,payload=\'{}\' WHERE id=%s AND user_id=%s',(UUID(parts[1]),user));return Reply('Диагностический доступ отозван.')
            if len(parts)!=1:raise ValueError('/diagnostic ID_операции — предоставить поддержке снимок одной операции на 15 минут; /diagnostic revoke ID_доступа — отозвать.')
            cfg=c.execute('SELECT telegram_user_id FROM admin_roles WHERE role=\'owner\' AND active LIMIT 1').fetchone()
            if not cfg:raise ValueError('Поддержка ещё не настроена.')
            op=c.execute('SELECT o.id,r.amount,r.occurred_on,r.description,o.kind,o.state FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=%s',(UUID(parts[0]),)).fetchone()
            if not op:raise ValueError('Операция недоступна.')
            payload={k:str(v) for k,v in op.items()}
            grant=c.execute('INSERT INTO diagnostic_grants(user_id,workspace_id,admin_telegram_id,operation_id,payload) VALUES(%s,current_workspace(),%s,%s,%s) RETURNING id',(user,cfg['telegram_user_id'],op['id'],Jsonb(payload))).fetchone()
            return Reply('Поддержка получит на 15 минут только этот снимок, без чеков и других операций:\n'+'\n'.join(f'{k}: {v}' for k,v in payload.items())+f"\nПолучатель: Telegram ID {cfg['telegram_user_id']}",[[('Разрешить на 15 минут',f"diagconfirm:{grant['id']}")]])
        return None

    def _admin_callback(self,c,user,callback):
        if not callback.startswith('diagconfirm:'):return None
        row=c.execute('UPDATE diagnostic_grants SET confirmed=true WHERE id=%s AND user_id=%s AND NOT revoked AND expires_at>now() RETURNING id',(UUID(callback.split(':')[1]),user)).fetchone()
        return Reply(f"Доступ разрешён. Отозвать: /diagnostic revoke {row['id']}" if row else 'Доступ истёк или отозван.')

    def admin_data(self,actor,section,search=''):
        with self._actor_transaction(actor) as c:
            role=c.execute('SELECT admin_role() AS role').fetchone()['role']
            if not role:raise ValueError('Нет доступа.')
            if role=='support' and section not in ('support','diagnostics'):raise ValueError('Недостаточно прав.')
            if section=='users':
                c.execute("SELECT set_config('balans.billing_worker','on',true)")
                return c.execute('SELECT s.telegram_user_id,s.status,s.created_at,s.last_seen,b.suspended,b.manual_until,(SELECT max(period_end) FROM billing_payments WHERE user_id=s.user_id AND success_seen AND NOT refunded) AS paid_until FROM service_users s LEFT JOIN billing_accounts b ON b.user_id=s.user_id WHERE (%s=\'\' OR s.telegram_user_id::text=%s) ORDER BY s.created_at DESC LIMIT 100',(search,search)).fetchall()
            if section=='support':return c.execute('SELECT * FROM support_tickets ORDER BY created_at DESC LIMIT 50').fetchall()
            if section=='diagnostics':return c.execute('SELECT id,user_id,operation_id,expires_at FROM diagnostic_grants WHERE admin_telegram_id=%s AND confirmed AND NOT revoked AND expires_at>now() ORDER BY created_at DESC LIMIT 50',(actor,)).fetchall()
            if section=='audit':return c.execute('SELECT actor_telegram_id,action,target,reason,created_at FROM admin_audit ORDER BY created_at DESC LIMIT 100').fetchall()
            if section=='ai':return c.execute('SELECT * FROM ai_configuration').fetchall()
            if section=='content':return c.execute('SELECT * FROM service_content ORDER BY key').fetchall()
            if section=='roles':return c.execute('SELECT * FROM admin_roles').fetchall()
            if section=='tariff':return c.execute('SELECT * FROM billing_config').fetchall()
            if section=='jobs':return c.execute('SELECT job_id,kind,state,model,prompt_version,error_code,input_tokens,output_tokens,updated_at FROM admin_job_metrics ORDER BY updated_at DESC LIMIT 100').fetchall()
            if section=='queue':
                c.execute("SELECT set_config('balans.inbox_worker','on',true)")
                return c.execute('SELECT bot_id,update_id,state,attempts,error_code,created_at,completed_at FROM telegram_inbox ORDER BY created_at DESC LIMIT 100').fetchall()
            c.execute("SELECT set_config('balans.billing_worker','on',true)")
            if section=='payments':return c.execute('SELECT telegram_user_id,charge_id,stars,period_start,period_end,refunded,success_seen FROM billing_payments ORDER BY created_at DESC LIMIT 100').fetchall()
            if section=='billing_audit':return c.execute('SELECT action,details,created_at FROM billing_audit ORDER BY created_at DESC LIMIT 100').fetchall()
            if section=='dashboard':
                result=dict(c.execute("SELECT count(*) AS registered,count(*) FILTER(WHERE last_seen>now()-interval '30 days') AS active_30d FROM service_users").fetchone(),**c.execute("SELECT count(DISTINCT user_id) FILTER(WHERE success_seen) AS paid_users,count(*) FILTER(WHERE success_seen) AS payments,coalesce(sum(stars) FILTER(WHERE success_seen AND NOT refunded),0) AS net_stars,coalesce(sum(stars) FILTER(WHERE refunded),0) AS refunded_stars,count(*) FILTER(WHERE refunded) AS refunds FROM billing_payments").fetchone(),**c.execute("SELECT count(*) AS jobs,count(*) FILTER(WHERE state='failed') AS failed_jobs,coalesce(sum(input_tokens),0) AS input_tokens,coalesce(sum(output_tokens),0) AS output_tokens FROM admin_job_metrics").fetchone())
                result['conversion_percent']=round(result['paid_users']*100/result['registered'],1) if result['registered'] else None
                result['renewals']=c.execute("SELECT count(*) AS n FROM billing_payments p JOIN billing_invoices i ON i.id=p.invoice_id WHERE p.success_seen AND p.charge_id<>i.first_charge_id").fetchone()['n']
                result['expired_payers']=c.execute("SELECT count(*) AS n FROM (SELECT user_id,max(period_end) AS expiry FROM billing_payments WHERE success_seen AND NOT refunded GROUP BY user_id) paid WHERE expiry<now()").fetchone()['n']
                result['estimated_ai_usd']=c.execute("SELECT coalesce(sum(estimated_usd),0) AS cost FROM ai_cost_reservations WHERE period_start=date_trunc('month',now() AT TIME ZONE 'UTC')::date AND state IN ('reserved','consumed')").fetchone()['cost']
                if c.execute('SELECT monthly_cost_limit_usd FROM ai_configuration').fetchone()['monthly_cost_limit_usd'] is None:result['estimated_ai_usd']=None
                return [result]
            raise ValueError('Раздел не найден.')

    def admin_mutate(self,actor,action,values):
        reason=values.get('reason','').strip()
        if not 3<=len(reason)<=1000:raise ValueError('Укажите причину изменения (3–1000 символов).')
        with self._actor_transaction(actor) as c:
            role=c.execute('SELECT admin_role() AS role').fetchone()['role']
            if not role:raise ValueError('Нет доступа.')
            if role=='support' and action!='support_reply':raise ValueError('Недостаточно прав.')
            target=values.get('target','').strip()
            if action in ('suspend','unsuspend','grant'):
                if role not in ('owner','admin'):raise ValueError('Недостаточно прав.')
                who=c.execute('SELECT * FROM service_users WHERE telegram_user_id=%s',(int(target),)).fetchone()
                if not who:raise ValueError('Пользователь не найден.')
                c.execute("SELECT set_config('balans.billing_worker','on',true)")
                c.execute('INSERT INTO billing_accounts(user_id,telegram_user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(who['user_id'],who['telegram_user_id']))
                if action=='grant':
                    days=int(values.get('days','0'))
                    if not 1<=days<=366:raise ValueError('Срок: 1–366 дней.')
                    c.execute("UPDATE billing_accounts SET manual_until=greatest(manual_until,now())+make_interval(days=>%s) WHERE user_id=%s",(days,who['user_id']))
                else:c.execute('UPDATE billing_accounts SET suspended=%s WHERE user_id=%s',(action=='suspend',who['user_id']))
            elif action=='support_reply':
                reply=values.get('reply','').strip()
                if not 1<=len(reply)<=3000:raise ValueError('Ответ: 1–3000 символов.')
                c.execute("UPDATE support_tickets SET reply=%s,state='answered' WHERE id=%s",(reply,UUID(target)))
            elif action=='queue_retry':
                if role not in ('owner','admin'):raise ValueError('Недостаточно прав.')
                bot_id,update_id=map(int,target.split(':'));c.execute("SELECT set_config('balans.inbox_worker','on',true)")
                row=c.execute("UPDATE telegram_inbox SET state='pending',attempts=0,available_at=now(),error_code=NULL WHERE bot_id=%s AND update_id=%s AND state='failed' AND payload<>'{}' RETURNING update_id",(bot_id,update_id)).fetchone()
                if not row:raise ValueError('Событие недоступно для повтора.')
            elif action=='content':
                if role!='owner':raise ValueError('Только владелец.')
                if target not in ('help_intro','support_intro','base_categories'):raise ValueError('Ключ: help_intro, support_intro, base_categories.')
                value=values.get('value','').strip()
                if not 1<=len(value)<=3500:raise ValueError('Текст: 1–3500 символов.')
                if target=='base_categories' and (len(value.splitlines())>20 or any(not 1<=len(x.strip())<=60 for x in value.splitlines())):raise ValueError('До 20 дополнительных категорий, по одной на строку, 1–60 символов.')
                c.execute('INSERT INTO service_content(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()',(target,value))
            elif action=='role':
                if role!='owner':raise ValueError('Только владелец.')
                desired=values.get('role')
                if desired not in ('admin','support','disabled'):raise ValueError('Роль: admin, support, disabled.')
                existing=c.execute('SELECT role FROM admin_roles WHERE telegram_user_id=%s',(int(target),)).fetchone()
                if existing and existing['role']=='owner':raise ValueError('Владельца нельзя изменить из панели.')
                c.execute('INSERT INTO admin_roles(telegram_user_id,role,active) VALUES(%s,%s,%s) ON CONFLICT(telegram_user_id) DO UPDATE SET role=excluded.role,active=excluded.active',(int(target),'support' if desired=='disabled' else desired,desired!='disabled'))
            elif action=='tariff':
                if role!='owner':raise ValueError('Только владелец.')
                if not c.execute('SELECT required FROM privacy_policy').fetchone()['required']:raise ValueError('Сначала настройте опубликованную политику приватности.')
                from scripts.configure_billing import configuration
                cfg=configuration({'BILLING_ENABLED':'true','OWNER_TELEGRAM_ID':str(actor),'SUBSCRIPTION_STARS':values.get('stars',''),'TRIAL_DAYS':values.get('trial_days','7'),'TEXT_MONTHLY_QUOTA':values.get('text_quota',''),'VOICE_MONTHLY_SECONDS':values.get('voice_seconds',''),'IMAGE_MONTHLY_QUOTA':values.get('image_quota',''),'TERMS_URL':values.get('terms_url',''),'SUPPORT_CONTACT':values.get('support_contact','')})
                from psycopg import sql
                c.execute(sql.SQL('UPDATE billing_config SET {},terms_version=terms_version+1,enabled_at=CASE WHEN enabled THEN enabled_at ELSE now() END').format(sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in cfg)),tuple(cfg.values()))
            elif action=='ai':
                if role!='owner':raise ValueError('Только владелец.')
                model=values.get('model','').strip();transcribe=values.get('transcribe_model','').strip();confidence=float(values.get('confidence','0.75'))
                if len(model)>100 or len(transcribe)>100 or not 0<=confidence<=1:raise ValueError('Проверьте модель и порог 0..1.')
                from decimal import Decimal,InvalidOperation
                rates={}
                for key in ('text_usd_per_request','voice_usd_per_minute','image_usd_per_file','monthly_cost_limit_usd'):
                    raw=values.get(key,'').strip()
                    try:value=Decimal(raw) if raw else None
                    except InvalidOperation:raise ValueError('Стоимость должна быть десятичным числом.') from None
                    if value is not None and (not value.is_finite() or value<=0):raise ValueError('Стоимость должна быть положительной.')
                    rates[key]=value
                if rates['monthly_cost_limit_usd'] and any(rates[k] is None for k in ('text_usd_per_request','voice_usd_per_minute','image_usd_per_file')):raise ValueError('Для лимита задайте оценочную стоимость всех трёх типов AI.')
                c.execute('UPDATE ai_configuration SET model=%s,transcribe_model=%s,confidence=%s,text_usd_per_request=%s,voice_usd_per_minute=%s,image_usd_per_file=%s,monthly_cost_limit_usd=%s,updated_at=now()',(model or None,transcribe or None,confidence,*rates.values()))
            else:raise ValueError('Действие не поддерживается.')
            c.execute('INSERT INTO admin_audit(actor_telegram_id,action,target,reason,details) VALUES(%s,%s,%s,%s,%s)',(actor,action,target,reason,Jsonb({k:v for k,v in values.items() if k not in ('csrf','otp','reply','value')})))
            return 'Изменение сохранено.'+(' Новые модели применятся после перезапуска бота.' if action=='ai' else '')

    def diagnostic_snapshot(self,actor,identity):
        with self._actor_transaction(actor) as c:
            grant=c.execute('SELECT * FROM diagnostic_grants WHERE id=%s AND admin_telegram_id=%s AND confirmed AND NOT revoked AND expires_at>now()',(UUID(identity),actor)).fetchone()
            if not grant:raise ValueError('Доступ истёк или отозван.')
            who=c.execute('SELECT telegram_user_id FROM service_users WHERE user_id=%s',(grant['user_id'],)).fetchone()
        with self._actor_transaction(who['telegram_user_id']) as c:
            if not c.execute('SELECT id FROM workspaces WHERE id=%s',(grant['workspace_id'],)).fetchone():raise ValueError('Автор больше не имеет доступа к бюджету.')
        with self._actor_transaction(actor) as c:
            current=c.execute('SELECT payload FROM diagnostic_grants WHERE id=%s AND confirmed AND NOT revoked AND expires_at>now()',(grant['id'],)).fetchone()
            if not current:raise ValueError('Доступ отозван.')
            c.execute("INSERT INTO admin_audit(actor_telegram_id,action,target,reason) VALUES(%s,'diagnostic_view',%s,'Явное согласие пользователя на снимок')",(actor,identity))
            return current['payload']

    def admin_log_external(self,actor,action,target,reason):
        if not 3<=len(reason.strip())<=1000:raise ValueError('Укажите причину действия.')
        with self._actor_transaction(actor) as c:
            if c.execute('SELECT admin_role() AS role').fetchone()['role']!='owner':raise ValueError('Только владелец.')
            c.execute('INSERT INTO admin_audit(actor_telegram_id,action,target,reason) VALUES(%s,%s,%s,%s)',(actor,action,target,reason))
