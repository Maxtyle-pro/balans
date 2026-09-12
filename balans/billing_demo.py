"""Allowlisted UI simulation, without Telegram invoices or payment ledger entries."""
from uuid import UUID
from psycopg.types.json import Jsonb
from balans.domain import Reply
from balans.onboarding import paid_quotas, DEMO_BADGE

BADGE=DEMO_BADGE


class BillingDemo:
    def _demo_enabled(self,c):
        return bool(c.execute('SELECT 1 FROM billing_demo WHERE user_id=actor_user_id() AND allowed AND enabled').fetchone())

    def _demo_state(self,c):
        return c.execute('SELECT d.* FROM billing_demo d JOIN workspaces w ON w.id=current_workspace() WHERE d.user_id=actor_user_id() AND d.allowed AND d.enabled AND w.kind=\'personal\' AND w.owner_user_id=actor_user_id()').fetchone()

    def _billing_config(self,c):
        cfg=c.execute('SELECT * FROM billing_config').fetchone()
        demo=self._demo_state(c)
        if demo:
            cfg=dict(cfg,enabled=True,stars=demo['stars'],trial_days=7,demo=True,demo_generation=str(demo['generation']),
                     terms_url='Имитация тарифа: реальные платежи и автосписания не выполняются.')
            # UI tests work even before the commercial tariff has been configured.
            for key,value in [('text_quota',1000),('voice_seconds',3600),('image_quota',100),('analysis_quota',30)]:
                if not cfg[key]:cfg[key]=value
        return cfg

    def _demo_panel(self,c):
        row=c.execute('SELECT * FROM billing_demo WHERE user_id=actor_user_id() AND allowed').fetchone()
        if not row:return Reply('Имитация оплаты не включена для этого аккаунта.')
        if not self._demo_state(c):
            return Reply('Имитация доступна только в личном бюджете. Выберите его и отправьте /demo on.',[[('Мои бюджеты','workspaces')]])
        gen=row['generation']
        return Reply(BADGE+'Условная цена: '+str(row['stars'])+' Stars за 30 дней.\n'
                     'Имитируется только оплата. Записи и обращения к ИИ работают обычно.\n'
                     '«Новый пользователь» сбрасывает только тестовую подписку, сохраняя операции.',
                     [[('Открыть старт','start'),('Моя подписка','subscription')],
                      [('Новый пользователь',f'demoreset:{gen}')],
                      [('Завершить период',f'demoexpire:{gen}')],[('Выйти из имитации','demooff')]])

    def _demo_command(self,c,user,command,arg,sent):
        if command!='/demo':return None
        if arg not in ('','on','off'):return Reply('/demo — тестовый пульт; /demo on — включить; /demo off — выйти.')
        if arg=='off':
            c.execute('UPDATE billing_demo SET enabled=false,checkout_state=CASE WHEN checkout_state=\'pending\' THEN \'cancelled\' ELSE checkout_state END WHERE user_id=%s',(user,))
            return Reply('Имитация выключена. Восстановлен обычный режим подписки.',[[('На старт','start')]])
        row=c.execute('SELECT * FROM billing_demo WHERE user_id=%s AND allowed',(user,)).fetchone()
        if not row:return Reply('Имитация оплаты не включена для этого аккаунта.')
        c.execute('UPDATE billing_demo SET enabled=true WHERE user_id=%s',(user,))
        return self._demo_panel(c)

    def _demo_callback(self,c,user,callback,sent):
        if callback=='demooff':return self._demo_command(c,user,'/demo','off',sent)
        if callback=='demopanel':return self._demo_panel(c)
        if not callback.startswith(('demobuy:','demopay:','democancel:','demoreset:','demoexpire:','demorenew:')):return None
        demo=self._demo_state(c)
        if not demo:return Reply('Имитация выключена или недоступна. /demo — тестовый режим.')
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if not access.get('demo'):return Reply('Тестовый доступ приостановлен. /support — поддержка.')
        action,raw=callback.split(':',1)
        if action in ('demopay','democancel'):
            try:identity=UUID(raw)
            except ValueError:return Reply('Недействительная тестовая кнопка. /demo')
            valid=c.execute("SELECT * FROM billing_demo WHERE user_id=%s AND checkout_id=%s AND checkout_at>now()-interval '15 minutes' FOR UPDATE",(user,identity)).fetchone()
            if not valid:return Reply('Тестовый счёт устарел. Откройте тариф снова.',[[('Моя подписка','subscription')]])
            if valid['checkout_state']!='pending':return self._subscription_card(c)
            if action=='democancel':
                c.execute("UPDATE billing_demo SET checkout_state='cancelled' WHERE user_id=%s",(user,))
                return Reply(BADGE+'Оплата отменена. Подписка не подключена.',[[('Моя подписка','subscription')],[('Тестовый пульт','demopanel')]])
            c.execute("UPDATE billing_demo SET paid_at=now(),paid_until=now()+interval '30 days',checkout_state='paid',auto_renew=true WHERE user_id=%s",(user,))
            reply=self._subscription_card(c)
            reply.text=BADGE+'Оплата успешно имитирована. Подписка активна 30 дней.\n\n'+reply.text.removeprefix(BADGE)
            return reply
        # Generation binds the control to the current simulation, including reset.
        value=raw
        renewal=None
        if action=='demorenew':
            value,_,renewal=raw.partition(':')
            if renewal not in ('0','1'):return Reply('Недействительная тестовая кнопка. /demo')
        if value!=str(demo['generation']):return Reply('Тестовая кнопка устарела. /demo — открыть пульт.')
        if action=='demoreset':
            c.execute('UPDATE billing_demo SET generation=gen_random_uuid(),trial_started_at=NULL,trial_until=NULL,trial_quotas=NULL,paid_at=NULL,paid_until=NULL,paid_quotas=NULL,checkout_id=NULL,checkout_at=NULL,checkout_state=NULL,auto_renew=true WHERE user_id=%s',(user,))
            return self._welcome(c)
        if action=='demoexpire':
            c.execute("UPDATE billing_demo SET generation=gen_random_uuid(),trial_started_at=coalesce(trial_started_at,now()-interval '7 days'),trial_until=now()-interval '1 second',paid_until=CASE WHEN paid_at IS NOT NULL THEN now()-interval '1 second' END,checkout_state='cancelled' WHERE user_id=%s",(user,))
            return self._subscription_card(c)
        if action=='demorenew':
            c.execute('UPDATE billing_demo SET auto_renew=%s WHERE user_id=%s',(renewal=='1',user))
            return self._demo_renewal(c)
        if access['status']=='active':return self._subscription_card(c)
        cfg=self._billing_config(c)
        if demo['checkout_state']!='pending' or not c.execute("SELECT 1 FROM billing_demo WHERE user_id=%s AND checkout_at>now()-interval '15 minutes'",(user,)).fetchone():
            demo=c.execute("UPDATE billing_demo SET checkout_id=gen_random_uuid(),checkout_at=now(),checkout_state='pending',paid_quotas=%s WHERE user_id=%s RETURNING *",(Jsonb(paid_quotas(cfg)),user)).fetchone()
        return Reply(BADGE+f"Подписка на 30 дней · условно {demo['stars']} Stars.\n"
                     'Нажмите «Оплатить тестово», чтобы увидеть успешное подключение. Настоящий счёт Telegram не создаётся.',
                     [[('Оплатить тестово',f"demopay:{demo['checkout_id']}")],[('Отменить',f"democancel:{demo['checkout_id']}")]])

    def _demo_renewal(self,c):
        demo=self._demo_state(c)
        if not demo or not demo['paid_at']:return self._subscription_card(c)
        return Reply(BADGE+'Автопродление: '+('включено' if demo['auto_renew'] else 'выключено')+'.\n'
                     'Это состояние для проверки интерфейса; списаний и автоматического продления в имитации нет.',
                     [[('Отключить автопродление' if demo['auto_renew'] else 'Включить автопродление',f"demorenew:{demo['generation']}:{0 if demo['auto_renew'] else 1}")],[('Назад','subscription')]])
