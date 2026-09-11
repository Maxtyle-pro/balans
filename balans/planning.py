"""Monthly limits and opt-in notification preferences, always scoped by actor RLS."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo
from balans.domain import Reply, amount_from_text, money,CURRENCY


def budget_period(day, start_day):
    start=day.replace(day=start_day)
    if day<start:start=(start.replace(day=1)-timedelta(days=1)).replace(day=start_day)
    following=(start.replace(day=28)+timedelta(days=4)).replace(day=start_day)
    return start,following-timedelta(days=1)


def minute(text):
    try:
        h,m=map(int,text.split(':'))
        if not 0<=h<24 or not 0<=m<60:raise ValueError()
        return h*60+m
    except ValueError:raise ValueError('Время: ЧЧ:ММ, например 19:00.') from None


def next_allowed(now, start, end, zone):
    if start is None or end is None or start==end:return now
    def quiet(t):
        local=t.astimezone(ZoneInfo(zone));m=local.hour*60+local.minute
        return start<=m<end if start<end else m>=start or m<end
    if not quiet(now):return now
    candidate=now.replace(second=0,microsecond=0)+timedelta(minutes=1)
    for _ in range(1600):
        if not quiet(candidate):return candidate
        candidate+=timedelta(minutes=1)
    return candidate


def latest_slot(now, zone, send_minute, weekday=None):
    local=now.astimezone(ZoneInfo(zone))
    slot=local.replace(hour=send_minute//60,minute=send_minute%60,second=0,microsecond=0)
    if slot>local:slot-=timedelta(days=1)
    if weekday is not None:slot-=timedelta(days=(slot.weekday()-weekday)%7)
    return slot.astimezone(timezone.utc)


class Planning:
    def _preference(self,c):
        c.execute('INSERT INTO notification_preferences(workspace_id,user_id,telegram_user_id) VALUES(current_workspace(),actor_user_id(),actor_telegram_id()) ON CONFLICT DO NOTHING')
        return c.execute('SELECT * FROM notification_preferences WHERE workspace_id=current_workspace() AND user_id=actor_user_id()').fetchone()

    def _budget_rows(self,c,now):
        settings=c.execute('SELECT timezone,budget_start_day FROM user_settings WHERE user_id=actor_user_id()').fetchone()
        start,end=budget_period(now.astimezone(ZoneInfo(settings['timezone'])).date(),settings['budget_start_day'])
        rows=c.execute("SELECT b.*,coalesce(cat.name,'Все расходы') AS name,coalesce((SELECT sum(CASE WHEN o.kind='refund' THEN -r.amount ELSE r.amount END) FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id LEFT JOIN operations parent ON parent.id=r.refund_of LEFT JOIN operation_revisions pr ON pr.id=parent.current_revision_id WHERE o.state='active' AND o.kind IN ('expense','refund') AND r.currency=b.currency AND r.occurred_on BETWEEN %s AND %s AND (b.category_id IS NULL OR CASE WHEN o.kind='refund' THEN pr.category_id ELSE r.category_id END=b.category_id)),0) AS spent FROM spending_budgets b LEFT JOIN categories cat ON cat.id=b.category_id WHERE b.active ORDER BY cat.name NULLS FIRST,b.id",(start,end)).fetchall()
        return start,end,rows

    def _budget_card(self,c,now):
        start,end,rows=self._budget_rows(c,now)
        text=f'Лимиты: {self._workspace_name(c)}\nПериод: {start:%d.%m.%Y}–{end:%d.%m.%Y}\n'
        for r in rows:text+=f"\n{r['name']}: {money(r['spent'],r['currency'])} / {money(r['amount'],r['currency'])}; остаток {money(r['amount']-r['spent'],r['currency'])}"
        text+='\n\n/budget 30000 — общий лимит\n/budget 10000 | Продукты — категория\n/budget off | Продукты — отключить\n/budgetday 5 — начало месяца (1–28)\n/notify — уведомления.\nЛимиты персональные; руководителю учитываются все видимые расходы общего бюджета. Возвраты уменьшают траты.'
        chunks=['']
        for line in text.splitlines(keepends=True):
            if len(chunks[-1])+len(line)>3500:chunks.append('')
            chunks[-1]+=line
        return Reply(chunks[0],messages=chunks[1:])

    def _planning_command(self,c,user,command,arg,sent):
        if command=='/budgetday':
            if not arg.isascii() or not arg.isdigit() or not 1<=int(arg)<=28:raise ValueError('/budgetday 1 — число от 1 до 28.')
            c.execute('UPDATE user_settings SET budget_start_day=%s WHERE user_id=%s',(int(arg),user))
            return self._budget_card(c,sent)
        if command=='/budget':
            if arg:
                parts=[x.strip() for x in arg.split('|')]
                if len(parts)>2:raise ValueError('/budget 10000 | Продукты')
                category=None
                if len(parts)==2:
                    row=c.execute('SELECT id FROM categories WHERE lower(name)=lower(%s) AND NOT archived',(parts[1],)).fetchone()
                    if not row:raise ValueError('Категория не найдена: /categories.')
                    category=row['id']
                if parts[0].lower()=='off':c.execute('UPDATE spending_budgets SET active=false WHERE category_id IS NOT DISTINCT FROM %s AND currency=%s',(category,CURRENCY.get()))
                else:
                    amount=amount_from_text(parts[0])
                    c.execute('INSERT INTO spending_budgets(workspace_id,author_user_id,category_id,amount,currency) VALUES(current_workspace(),%s,%s,%s,%s) ON CONFLICT(workspace_id,author_user_id,category_id,currency) DO UPDATE SET amount=excluded.amount,active=true',(user,category,amount,CURRENCY.get()))
            return self._budget_card(c,sent)
        if command!='/notify':return None
        pref=self._preference(c);parts=arg.lower().split()
        if parts:
            updates={}
            if parts in (['on'],['off']):
                updates={'enabled':parts[0]=='on','blocked':False}
                if parts[0]=='on' and not pref['enabled']:updates['enabled_at']=sent
            elif len(parts)==2 and parts[0] in ('budget','reminder','weekly') and parts[1] in ('on','off'):
                updates[{'budget':'budget_alerts','reminder':'reminder','weekly':'weekly'}[parts[0]]] = parts[1]=='on'
            elif len(parts)==2 and parts[0]=='events' and parts[1] in ('off','instant','daily'):updates['shared_mode']=parts[1]
            elif len(parts)==2 and parts[0]=='types':
                values=list(dict.fromkeys(parts[1].split(',')))
                if not set(values)<= {'expense','funds','review'}:raise ValueError('Типы: expense,funds,review')
                updates['event_types']=values
            elif len(parts)==2 and parts[0]=='time':updates['send_minute']=minute(parts[1])
            elif len(parts)==2 and parts[0]=='weekday' and parts[1] in list('0123456'):updates['weekday']=int(parts[1])
            elif parts==['quiet','off']:updates={'quiet_start':None,'quiet_end':None}
            elif len(parts)==3 and parts[0]=='quiet':updates={'quiet_start':minute(parts[1]),'quiet_end':minute(parts[2])}
            else:raise ValueError('Неизвестная настройка. /notify — примеры.')
            from psycopg import sql
            c.execute(sql.SQL('UPDATE notification_preferences SET {},next_check_at=now() WHERE id=%s').format(sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in updates)),(*updates.values(),pref['id']))
            pref=self._preference(c)
            # Never revive pending events after disabling and later re-enabling.
            if 'event_types' in updates or updates.get('shared_mode')=='off':
                c.execute("UPDATE notification_outbox SET state='cancelled' WHERE workspace_id=current_workspace() AND state='pending' AND kind IN ('event','digest')")
                c.execute('UPDATE notification_events SET processed=true WHERE workspace_id=current_workspace() AND NOT processed AND NOT (event_type=ANY(%s))',(pref['event_types'],))
            if updates.get('shared_mode')=='off':c.execute('UPDATE notification_events SET processed=true WHERE workspace_id=current_workspace() AND NOT processed')
            if not pref['enabled']:
                c.execute("UPDATE notification_outbox SET state='cancelled' WHERE workspace_id=current_workspace() AND state='pending'")
                c.execute('UPDATE notification_events SET processed=true WHERE workspace_id=current_workspace() AND NOT processed')
        fmt=lambda n:'выключены' if n is None else f'{n//60:02}:{n%60:02}'
        return Reply(f"Уведомления: {self._workspace_name(c)}\nДоставка: {'включена' if pref['enabled'] else 'выключена'}\nЛимиты 80/100%: {pref['budget_alerts']}\nНапоминание: {pref['reminder']}\nНедельный отчёт: {pref['weekly']}\nСобытия: {pref['shared_mode']} ({','.join(pref['event_types'])})\nВремя: {fmt(pref['send_minute'])}, день недели: {pref['weekday']} (0 = пн)\nТихие часы: {fmt(pref['quiet_start'])}–{fmt(pref['quiet_end'])}\nЧасовой пояс — из /settings.\n\n/notify on или off — вся доставка\n/notify budget on\n/notify reminder on\n/notify weekly on\n/notify events instant или daily или off\n/notify types expense,funds,review\n/notify time 19:00\n/notify weekday 0\n/notify quiet 22:00 09:00\n/notify quiet off\nНастройки действуют для выбранного бюджета.")
