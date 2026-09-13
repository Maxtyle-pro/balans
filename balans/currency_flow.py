"""Explicit native currencies and manually sourced FX. Never add unlike currencies."""
from datetime import date
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from uuid import UUID
from psycopg.types.json import Jsonb
from balans.domain import Reply,amount_from_text,date_from_text,CURRENCY,money
from balans.report_data import summarize,amount

class CurrencyFlow:
    def _currency_picker(self,c):
        return Reply('💱 Выберите валюту учёта\nОна будет использоваться для новых расходов, доходов и чеков. Повторно указывать её не нужно.',[[('₽ Рубли','usercurrency:RUB')],[('$ Доллары','usercurrency:USD')],[('€ Евро','usercurrency:EUR')]])

    def _user_currency_callback(self,c,callback):
        if callback=='currencysettings':return self._currency_picker(c)
        if not callback.startswith('usercurrency:'):return None
        code=callback.split(':')[1]
        c.execute('SELECT choose_user_currency(%s)',(code,))
        reply=self._welcome(c)
        reply.text='✅ Валюта учёта: '+code+'.\n\n'+reply.text
        return reply

    def _currency_command(self,c,user,command,arg,sent):
        if command in ('/account','/workspace') and '|' in arg:
            setting=c.execute('SELECT currency,currency_selected_at FROM user_settings WHERE user_id=actor_user_id()').fetchone()
            if setting['currency_selected_at'] and arg.split('|')[-1].strip().upper()!=setting['currency']:
                return Reply('Используется единая валюта учёта: '+setting['currency']+'. Её можно посмотреть в настройках.',[[('Настройки валюты','currencysettings')]])
        if command=='/account' and '|' in arg:
            parts=[p.strip() for p in arg.split('|')]
            if len(parts)!=2:raise ValueError('/account Доллары | USD')
            identity=self._workspace_call(c,'SELECT create_currency_account(%s,%s) AS id',(parts[0],parts[1].upper()))['id']
            return Reply('Счёт создан: '+parts[0]+' · '+parts[1].upper(),[[('Использовать этот счёт',f'acuse:{identity}')]])
        if command=='/workspace' and '|' in arg:
            if self._workspace_busy(c):raise ValueError('Сначала завершите текущий ввод.')
            parts=[p.strip() for p in arg.split('|')]
            if len(parts)!=2:raise ValueError('/workspace Закупки | USD')
            self._workspace_call(c,'SELECT create_currency_workspace(%s,%s)',(parts[0],parts[1].upper()))
            return self._workspace_list(c)
        if command=='/exchange':
            parts=[p.strip() for p in arg.split('|')]
            if len(parts) not in (3,4):raise ValueError('/exchange 100 | Доллары | 1.05 | сегодня — списать 100 с выбранного счёта и зачислить 1.05 на счёт «Доллары». Комиссия — отдельный расход.')
            target=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.user_id=actor_user_id() AND lower(a.name)=lower(%s)',(parts[1],)).fetchone()
            if not target:raise ValueError('Счёт получателя не найден.')
            received=amount_from_text(parts[2])
            if self._workspace_busy(c):raise ValueError('Сначала завершите текущий ввод.')
            self._finance_new(c,user,sent,'transfer',parts[0],'Обмен между своими счетами',parts[3] if len(parts)==4 else None,target['id'])
            d=self._draft(c)
            c.execute('UPDATE operation_drafts SET destination_amount=%s WHERE id=%s',(received,d['id']))
            return self._finance_prompt(c,self._draft(c))
        if command=='/fx':
            if not arg:return Reply('/fx USD | 95.50 | сегодня — ручной курс: 1 USD = 95.50 единиц базовой валюты бюджета. Применяется к новым операциям с этой даты; уже сохранённый курс не переписывается. Без курса суммы валют считаются раздельно.')
            parts=[p.strip() for p in arg.split('|')]
            if len(parts)!=3:raise ValueError('/fx USD | 95.50 | сегодня')
            cur=parts[0].upper();ctx=self._account_context(c);base=c.execute('SELECT base_currency FROM workspaces WHERE id=current_workspace()').fetchone()['base_currency']
            if cur==base or not c.execute('SELECT code FROM currencies WHERE code=%s',(cur,)).fetchone():raise ValueError('Укажите другую поддерживаемую валюту: RUB, USD или EUR.')
            try:rate=Decimal(parts[1].replace(',','.'))
            except InvalidOperation:raise ValueError('Курс должен быть десятичным числом.') from None
            if not rate.is_finite() or not 0<rate<100000000 or rate.as_tuple().exponent < -10:raise ValueError('Положительный курс до 100000000, максимум 10 знаков после запятой.')
            day=date_from_text(parts[2],sent,ctx['timezone'])
            c.execute('INSERT INTO exchange_rates(workspace_id,author_user_id,currency,base_currency,rate,effective_on) VALUES(current_workspace(),%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,author_user_id,currency,base_currency,effective_on) DO UPDATE SET rate=excluded.rate',(user,cur,base,rate,day))
            return Reply(f'Ручной курс сохранён: 1 {cur} = {rate} {base}, дата {day}. Исторические операции не пересчитаны.')
        return None

    def _report_snapshot(self,c,user_id,arg,sent_at):
        parts=[p.strip() for p in arg.split('|')];rest=[parts[0]];chosen=None;convert=None
        for p in parts[1:]:
            if p.startswith('currency='):chosen=p.split('=',1)[1].upper()
            elif p.startswith('convert='):convert=p.split('=',1)[1].upper()
            else:rest.append(p)
        chosen=chosen or self._account_context(c)['currency']
        if chosen!='ALL' and not c.execute('SELECT code FROM currencies WHERE code=%s',(chosen,)).fetchone():raise ValueError('Валюта отчёта: RUB, USD, EUR или all.')
        currencies=[r['currency'] for r in c.execute('SELECT DISTINCT currency FROM accounts ORDER BY currency').fetchall()] if chosen=='ALL' else [chosen]
        reports=[]
        for currency in currencies:
            c.execute("SELECT set_config('balans.report_currency',%s,true)",(currency,))
            report=super()._report_snapshot(c,user_id,' | '.join(rest),sent_at);reports.append(report)
        if len(reports)==1 and not convert:return reports[0]
        snapshots=[r['snapshot'] for r in reports];root=dict(snapshots[0]);root['rows']=[row for s in snapshots for row in s['rows']];root['previous_rows']=[row for s in snapshots for row in s.get('previous_rows',[])]
        if convert:
            base=c.execute('SELECT base_currency FROM workspaces WHERE id=current_workspace()').fetchone()['base_currency']
            if convert!=base:raise ValueError('Пересчёт доступен только в базовую валюту '+base+'.')
            for key in ('rows','previous_rows'):
                converted=[]
                for row in root[key]:
                    rate=Decimal(1) if row['currency']==base else Decimal(row['exchange_rate']) if row.get('exchange_rate') else None
                    if row['kind']=='transfer':continue
                    if rate is None:raise ValueError('Нет сохранённого курса для части операций. Просматривайте валюты отдельно: /report all | currency=all. /fx — курсы для новых операций.')
                    converted.append(dict(row,original_amount=row['amount'],original_currency=row['currency'],amount=str((Decimal(row['amount'])*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),currency=base))
                root[key]=converted
            root['currency']=base;root['converted']=True;root.pop('funds',None)
            root['summary']=summarize(root['rows'],date.fromisoformat(root['start']),date.fromisoformat(root['end']))
            root['previous']=summarize(root['previous_rows'],date.fromisoformat(root['previous_start']),date.fromisoformat(root['previous_end']))
            delta=amount(root['summary']['total'])-amount(root['previous']['total']);root['delta']=str(delta);root['delta_percent']=str((delta*100/amount(root['previous']['total'])).quantize(Decimal('.1'))) if amount(root['previous']['total']) else None
        else:
            root['currency']='MULTI';root['currency_reports']=snapshots
            root['summary']={'count':sum(s['summary']['count'] for s in snapshots),'total':'0'};root.pop('funds',None)
        return c.execute('INSERT INTO reports(workspace_id,author_user_id,snapshot) VALUES(current_workspace(),%s,%s) RETURNING *',(user_id,Jsonb(root))).fetchone()

    def _report_card(self,report):
        s=report['snapshot']
        if not s.get('currency_reports'):
            reply=super()._report_card(report);currency=s.get('currency','RUB')
            if currency!='RUB':reply.text=reply.text.replace('₽',currency)
            reply.text='Валюта: '+currency+' · суммы других валют не включены.\n'+reply.text
            if s.get('converted'):reply.text='Пересчитано по сохранённым ручным курсам; даты и источники — в CSV.\n'+reply.text
            return reply
        lines=[f"Отчёт {s['start']} — {s['end']}. Валюты раздельно."]
        for child in s['currency_reports']:
            q=child['summary'];currency=child['currency']
            lines.append(f"{currency}: расходы {money(Decimal(q['total']),currency)}, возвраты {money(Decimal(q['refunds']),currency)}, чистые расходы {money(Decimal(q['net_expenses']),currency)}, доходы {money(Decimal(q['income']),currency)}")
        identity=report['id'];return Reply('\n'.join(lines)+'\nОбщий итог разных валют не вычисляется.',[[('PDF',f'rpdf:{identity}'),('CSV',f'rcsv:{identity}')],[('Поделиться',f'rshare:{identity}')]])

    def _analysis_consent(self,report):
        if report['snapshot'].get('currency_reports'):return Reply('Для AI-анализа выберите одну валюту: /analyze месяц | currency=USD. Суммы разных валют не объединяются.')
        return super()._analysis_consent(report)
