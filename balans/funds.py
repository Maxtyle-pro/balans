"""Shared money movement: plans, transit, receipt and explicit reconciliation."""
from uuid import UUID
from datetime import datetime
from decimal import Decimal
from psycopg.types.json import Jsonb
from balans.domain import Reply,amount_from_text,date_from_text,money

STATES={'planned':'План — деньги ещё не отправлены','sent':'В пути','received':'Получено','cancelled':'Отменено'}

class Funds:
    def _fund_confirmation(self,c,payload,text):
        row=c.execute('INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s) RETURNING id',(Jsonb(payload),)).fetchone()
        return Reply('Бюджет: '+self._workspace_name(c)+'\n'+text+'\nПока не подтверждено.',[[('Подтвердить',f"fcconfirm:{row['id']}"),('Отмена',f"fcdismiss:{row['id']}")]])

    def _fund_card(self,c,f):
        self._document_set(c,'transfer',f['id'])
        actor=c.execute('SELECT actor_user_id() AS id').fetchone()['id'];suffix=f"{f['id']}:{f['version']}"
        text=f"{STATES[f['state']]}\nID: {f['id']}\nОт {f['sender_telegram_id']} → {f['recipient_telegram_id']}\nСумма: {money(f['amount'])}; получено: {money(f['received'])}\nДата отправки: {f['occurred_on']}\nНазначение: {f['purpose']}"
        if f['due_on']:text+='\nСрок отчёта: '+str(f['due_on'])
        if f['dispute']:text+='\nРасхождение: '+f['dispute']
        buttons=[]
        if f['sender_user_id']==actor and f['state']=='planned':buttons=[[('Деньги отправлены',f'fcsend:{suffix}'),('Отменить план',f'fccancel:{suffix}')]]
        if f['recipient_user_id']==actor and f['state']=='sent':
            buttons=[[('Получил весь остаток',f'fcget:{suffix}')]]
            text+=f"\nЧастично: /receive {f['id']} | сумма\nРасхождение: /dispute {f['id']} | причина"
        buttons.append([('Документы передачи',f"docview:transfer:{f['id']}")])
        return Reply('Бюджет: '+self._workspace_name(c)+'\n'+text,buttons)

    def _funds_command(self,c,user,command,arg,sent):
        if command not in ('/issue','/returnfunds','/funds','/receive','/dispute','/claim','/reconcile'):return None
        w=c.execute("SELECT *,owner_user_id=actor_user_id() AS owner FROM workspaces WHERE id=current_workspace() AND kind='shared'").fetchone()
        if not w:return Reply('Выберите совместный бюджет: /workspaces.')
        parts=[x.strip() for x in arg.split('|')]
        context=self._account_context(c)
        if command in ('/issue','/returnfunds'):
            if command=='/issue':
                if not w['owner']:return Reply('Выдачу создаёт руководитель.')
                if len(parts) not in (4,5):return Reply('/issue Telegram_ID | сумма | дата | назначение | срок отчёта (необязательно)')
                recipient=int(parts.pop(0))
            else:
                if w['owner']:return Reply('Руководитель выдаёт средства через /issue.')
                if len(parts)!=3:return Reply('/returnfunds сумма | дата | назначение — возврат руководителю.')
                recipient=0
            value=amount_from_text(parts[0]);day=date_from_text(parts[1],sent,context['timezone'])
            due=datetime.strptime(parts[3],'%d.%m.%Y').date() if len(parts)>3 else None
            f=self._workspace_call(c,'SELECT create_fund_transfer(%s,%s,%s,%s,%s,%s) AS id',(recipient,context['account_id'],value,day,parts[2],due))
            return self._fund_card(c,c.execute('SELECT * FROM fund_transfers WHERE id=%s',(f['id'],)).fetchone())
        if command=='/funds' and arg.startswith('summary'):
            page=int(arg.partition('=')[2] or '1')
            if not 1<=page<=100000:raise ValueError('/funds summary=2 — следующая страница.')
            members=c.execute("SELECT id,user_id,telegram_user_id FROM memberships WHERE workspace_id=current_workspace() AND role='participant' ORDER BY telegram_user_id LIMIT 5 OFFSET %s",((page-1)*5,)).fetchall()
            lines=[]
            for m in members:
                sums=c.execute("SELECT coalesce(sum(amount) FILTER(WHERE recipient_user_id=%s AND state IN ('sent','received')),0) AS issued,coalesce(sum(received) FILTER(WHERE recipient_user_id=%s),0) AS received,coalesce(sum(amount-received) FILTER(WHERE recipient_user_id=%s AND state='sent'),0) AS transit,coalesce(sum(amount) FILTER(WHERE sender_user_id=%s AND state IN ('sent','received')),0) AS returned FROM fund_transfers",(m['user_id'],)*4).fetchone()
                claims=c.execute("SELECT coalesce(sum(amount) FILTER(WHERE state='pending'),0) AS pending,coalesce(sum(amount) FILTER(WHERE state='external'),0) AS external FROM fund_claims WHERE author_user_id=%s",(m['user_id'],)).fetchone()
                ops=c.execute("SELECT coalesce(sum(r.amount) FILTER(WHERE o.kind='expense'),0) AS spent,coalesce(sum(r.amount) FILTER(WHERE o.kind='refund'),0) AS refunds,coalesce(sum(CASE WHEN r.opening_negative THEN -r.amount ELSE r.amount END) FILTER(WHERE o.kind='opening'),0) AS opening FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.created_by_user_id=%s AND o.state='active'",(m['user_id'],)).fetchone()
                balance=ops['opening']+sums['received']+claims['external']+ops['refunds']-ops['spent']-sums['returned']
                lines.append(f"Участник {m['telegram_user_id']}\nВыдано: {money(sums['issued'])}; в пути: {money(sums['transit'])}; получено: {money(sums['received'])}\nНесверенные приходы: {money(claims['pending'])}; внешние: {money(claims['external'])}\nРасходы: {money(ops['spent'])}; возвраты покупок: {money(ops['refunds'])}; возвращено руководителю: {money(sums['returned'])}\nСверенный остаток: {money(balance)}; заявленный: {money(balance+claims['pending'])}")
            return Reply('Сводка участников · '+w['name']+'\n\n'+'\n\n'.join(lines)+f'\n/funds summary={page+1} — далее. /funds — передачи. Это учётные остатки.')
        if command=='/funds':
            if arg and not arg.startswith('page='):
                f=c.execute('SELECT * FROM fund_transfers WHERE id=%s',(UUID(arg),)).fetchone()
                return self._fund_card(c,f) if f else Reply('Передача недоступна.')
            page=int(arg[5:]) if arg else 1
            if not 1<=page<=100000:raise ValueError('/funds page=2 — номер страницы от 1.')
            rows=c.execute('SELECT * FROM fund_transfers ORDER BY created_at DESC,id DESC LIMIT 5 OFFSET %s',((page-1)*5,)).fetchall()
            claims=c.execute("SELECT * FROM fund_claims ORDER BY created_at DESC,id DESC LIMIT 5 OFFSET %s",((page-1)*5,)).fetchall()
            transit=sum((f['amount']-f['received'] for f in rows if f['state']=='sent'),Decimal(0))
            # All current visible records, rather than only the displayed page.
            transit=c.execute("SELECT coalesce(sum(amount-received),0) AS value FROM fund_transfers WHERE state='sent'").fetchone()['value']
            text='Движение средств · '+w['name']+'\nВ пути: '+money(transit)+'\n/funds summary — сводка по участникам.\n/accounts — сверенные и заявленные остатки.\nПередачи (5 на странице):\n'+'\n'.join(f"{f['sender_telegram_id']} → {f['recipient_telegram_id']}: {money(f['amount'])} · {STATES[f['state']]}" for f in rows)
            text+='\nЗаявленные приходы (5 на странице):\n'+'\n'.join(f"{cl['id']} · {cl['author_telegram_id']} · {money(cl['amount'])} · {cl['state']} · {cl['source'][:50]} · {cl['purpose'][:60]}" for cl in claims)
            text+=f'\n/funds page={page+1} — следующая страница; /funds — начало.\n/claim ID — сведения о заявленном приходе.'
            if w['owner']:text+='\n/reconcile ID_прихода | external / rejected / matched | причина | ID_выдачи (для matched)'
            return Reply(text, [[(f"Передача {i}",f"fcview:{f['id']}")] for i,f in enumerate(rows,1)])
        if command in ('/receive','/dispute'):
            if len(parts)!=2:return Reply(command+' ID_передачи | '+('сумма' if command=='/receive' else 'причина'))
            f=c.execute("SELECT * FROM fund_transfers WHERE id=%s AND recipient_user_id=%s AND state='sent'",(UUID(parts[0]),user)).fetchone()
            if not f:return Reply('Передача недоступна.')
            payload={'action':'receive' if command=='/receive' else 'dispute','id':str(f['id']),'version':f['version'],'account':str(context['account_id'])}
            if command=='/receive':payload['amount']=str(amount_from_text(parts[1]));description='Получено фактически: '+money(Decimal(payload['amount']))
            else:payload['note']=parts[1][:500];description='Расхождение: '+payload['note']
            return self._fund_confirmation(c,payload,description+'\nПередача: '+str(f['id']))
        if command=='/claim':
            if len(parts)==1 and len(arg)==36:
                cl=c.execute('SELECT * FROM fund_claims WHERE id=%s',(UUID(arg),)).fetchone()
                if not cl:return Reply('Приход недоступен.')
                return Reply(f"Заявленный приход {cl['id']}\n{cl['state']} · {money(cl['amount'])} · {cl['occurred_on']}\nИсточник: {cl['source']}\nНазначение: {cl['purpose']}\nОснование сверки: {cl['reason'] or '—'}\nСвязанная выдача: {cl['transfer_id'] or '—'}")
            if len(parts)!=4:return Reply('/claim сумма | дата | источник | назначение — заявить фактически полученные средства для сверки.')
            payload={'action':'claim','account':str(context['account_id']),'amount':str(amount_from_text(parts[0])),'date':str(date_from_text(parts[1],sent,context['timezone'])),'source':parts[2],'note':parts[3]}
            if not 1<=len(parts[2])<=200 or not 1<=len(parts[3])<=500:raise ValueError('Источник: 1–200 символов; назначение: 1–500.')
            open_issues=c.execute("SELECT id,amount-received AS remaining FROM fund_transfers WHERE recipient_user_id=actor_user_id() AND state='sent' ORDER BY created_at LIMIT 10").fetchall()
            if open_issues:
                reply=self._fund_confirmation(c,payload,f"Заявить отдельное получение {money(Decimal(payload['amount']))} от {parts[2]}: {parts[3]}? Если это открытая выдача, подтвердите её через /receive вместо этой записи.")
                reply.text+='\nСначала сопоставьте получение с открытой выдачей. Это предотвращает двойной приход:\n'+'\n'.join(f"/receive {f['id']} | {f['remaining']}" for f in open_issues)+'\nПодтверждайте отдельный приход только для другого фактического получения.'
                return reply
            return self._fund_confirmation(c,payload,f"Заявляю фактическое получение {money(Decimal(payload['amount']))}\n{payload['date']} · {parts[2]} · {parts[3]}\nДо сверки влияет только на заявленный остаток.")
        if len(parts) not in (3,4) or parts[1] not in ('external','matched','rejected'):return Reply('/reconcile ID_прихода | external / matched / rejected | основание | ID_выдачи (для matched)')
        return self._fund_confirmation(c,{'action':'reconcile','id':str(UUID(parts[0])),'decision':parts[1],'note':parts[2],'transfer':str(UUID(parts[3])) if len(parts)==4 else None},'Сверить приход '+parts[0]+'\nРешение: '+parts[1]+'\nОснование: '+parts[2])

    def _funds_callback(self,c,user,callback,sent):
        action,_,raw=callback.partition(':')
        if action not in ('fcview','fcsend','fccancel','fcget','fcconfirm','fcdismiss'):return None
        if action in ('fcconfirm','fcdismiss'):
            r=c.execute("SELECT * FROM fund_confirmations WHERE id=%s AND expires_at>now() FOR UPDATE",(UUID(raw),)).fetchone()
            if not r:return Reply('Подтверждение недоступно или истекло.')
            if r['state']=='cancelled':return Reply('Подтверждение отменено.')
            if r['state']=='done':return Reply('Уже подтверждено. Повторной записи нет.')
            if action=='fcdismiss':
                c.execute("UPDATE fund_confirmations SET state='cancelled' WHERE id=%s",(r['id'],))
                return Reply('Подтверждение отменено.')
            p=r['payload']
            if p.get('action') not in ('claim','reconcile','receive','dispute'):return Reply('Эта кнопка относится к другому действию.')
            if p['action']=='claim':
                claim=self._workspace_call(c,'SELECT create_fund_claim(%s,%s,%s,%s,%s) AS id',(UUID(p['account']),Decimal(p['amount']),p['date'],p['source'],p['note']))
                self._document_set(c,'claim',claim['id'])
            elif p['action']=='reconcile':self._workspace_call(c,'SELECT reconcile_claim(%s,%s,%s,%s)',(UUID(p['id']),p['decision'],p['note'],UUID(p['transfer']) if p['transfer'] else None))
            else:self._workspace_call(c,'SELECT fund_action(%s,%s,%s,%s,%s,%s)',(UUID(p['id']),p['action'],p['version'],Decimal(p['amount']) if p.get('amount') else None,UUID(p['account']),p.get('note')))
            c.execute("UPDATE fund_confirmations SET state='done' WHERE id=%s",(r['id'],))
            return Reply('Подтверждено. /funds — движение средств; /accounts — остатки.')
        identity,_,version=raw.partition(':')
        f=c.execute('SELECT * FROM fund_transfers WHERE id=%s',(UUID(identity),)).fetchone()
        if not f:return Reply('Передача недоступна.')
        if action=='fcview':return self._fund_card(c,f)
        if action=='fcget':
            if f['version']!=int(version):return Reply('Карточка устарела; /funds.')
            context=self._account_context(c)
            return self._fund_confirmation(c,{'action':'receive','id':identity,'version':f['version'],'amount':str(f['amount']-f['received']),'account':str(context['account_id'])},'Подтвердить фактическое получение '+money(f['amount']-f['received'])+'?')
        self._workspace_call(c,'SELECT fund_action(%s,%s,%s)',(f['id'],'send' if action=='fcsend' else 'cancel',int(version)))
        return self._fund_card(c,c.execute('SELECT * FROM fund_transfers WHERE id=%s',(f['id'],)).fetchone())
