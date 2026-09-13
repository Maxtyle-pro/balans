from datetime import date
from zoneinfo import ZoneInfo
from decimal import Decimal
from uuid import UUID
from psycopg.types.json import Jsonb
from psycopg.errors import RaiseException
from balans.domain import Reply,money
from balans.text_input import parse_items
from balans.finance import KINDS


class InputFlow:
    def _input_batch(self,c):
        c.execute("UPDATE input_batches SET state='cancelled' WHERE state='active' AND expires_at<=now()")
        return c.execute("SELECT * FROM input_batches WHERE state='active'").fetchone()

    def _batch_card(self,c,batch):
        if not batch:return Reply('Нет списка для ввода. Отправьте, например: «Обед 600; метро 70; аптека 430».')
        lines=[];buttons=[];pending=0;total=Decimal(0)
        for i,item in enumerate(batch['items']):
            state='excluded' if item.get('excluded') else 'pending'
            if item.get('draft_id'):
                draft=c.execute('SELECT * FROM operation_drafts WHERE id=%s',(UUID(item['draft_id']),)).fetchone()
                state=draft['state'] if draft else 'cancelled'
                if draft:
                    item={**item,'amount':str(draft['amount']),'description':draft['description'] or '', 'date':str(draft['occurred_on']),'kind':draft['kind']}
            status={'pending':'к проверке','saved':'сохранено','cancelled':'отменено','excluded':'исключено'}[state]
            if state=='pending':
                pending+=1;buttons.append([(f'Проверить №{i+1}',f"titem:{batch['id']}:{i}"),(f'Исключить №{i+1}',f"tdrop:{batch['id']}:{i}")])
            if state in ('pending','saved'):total+=Decimal(item['amount'])
            lines.append(f"{i+1}. {KINDS[item['kind']]} · {money(Decimal(item['amount']))} · {item['date']}\n{item['description'][:100]} — {status}")
        if not pending:c.execute("UPDATE input_batches SET state='done' WHERE id=%s",(batch['id'],))
        return Reply('Список операций\n\n'+'\n\n'.join(lines)+f'\n\nСумма выбранных строк: {money(total)}. Доходы и расходы учитываются раздельно. Каждая строка сохраняется после её подтверждения.\n/batch — вернуться к списку; /batch cancel — отменить оставшееся.',buttons)

    def _text_create_draft(self,c,user,sent,row,item):
        c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,amount,description,occurred_on,timezone_snapshot,source_sent_at,step,flow,kind) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'confirm','auto',%s)",
                  (row['id'],user,row['account_id'],item['amount'],item['description'],item['date'] or sent.astimezone(ZoneInfo(row['timezone'])).date(),row['timezone'],sent,'income' if item['kind']=='incoming' else item['kind']))
        d=self._draft(c)
        c.execute('UPDATE operation_drafts SET automatic_capture=%s,capture_warnings=%s WHERE id=%s',(self._capture_enabled(c),item.get('capture_warnings',[]),d['id']))
        d=self._draft(c)
        if item.get('recognized'):
            c.execute('UPDATE operation_drafts SET category_id=%s,capture_kind_pending=%s WHERE id=%s',(UUID(item['category_id']) if item.get('category_id') and item['kind']=='expense' else None,item['kind']=='incoming',d['id']))
            if not d['automatic_capture']:
                c.execute("UPDATE operation_drafts SET step=CASE WHEN amount IS NULL THEN 'amount' WHEN kind='expense' AND category_id IS NULL THEN 'category' ELSE 'confirm' END WHERE id=%s",(d['id'],))
            reply=self._prompt(c,self._draft(c));reply.capture_draft_id=str(d['id']);return reply
        reply=self._suggest(c,d) if item['kind']=='expense' else (self._capture_draft(c,d) or self._finance_prompt(c,d))
        reply.capture_draft_id=str(d['id'])
        return reply

    def _free_text(self,c,user,text,sent):
        batch=self._input_batch(c)
        if batch:return Reply('Продолжаю список…',capture_batch_id=str(batch['id'])) if self._capture_enabled(c) else self._batch_card(c,batch)
        if self._voice_busy(c) or c.execute("SELECT id FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone():return Reply('Сначала завершите обработку или /cancel.')
        if self._capture_enabled(c) or self.text_ai.available:return self._text_recognize(c,user,text,sent)
        row=self._account_context(c);items=parse_items(text,sent,row['timezone'])
        if any(item['kind']=='income' for item in items) and c.execute("SELECT kind='shared' AS shared FROM workspaces WHERE id=current_workspace()").fetchone()['shared']:
            return Reply('Получение в совместном бюджете требует сверки: /funds — открытые выдачи; /claim сумма | дата | источник | назначение. Расходы отправьте отдельно.')
        if len(items)==1:return self._text_create_draft(c,user,sent,row,items[0])
        batch=c.execute('INSERT INTO input_batches(workspace_id,author_user_id,account_id,timezone_snapshot,source_sent_at,items) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(row['id'],user,row['account_id'],row['timezone'],sent,Jsonb(items))).fetchone()
        return Reply('Записываю операции…',capture_batch_id=str(batch['id'])) if self._capture_enabled(c) else self._batch_card(c,batch)

    def _input_callback(self,c,user,callback,sent):
        if callback.startswith('edit:'):
            _,identity,version=callback.split(':')
            d=self._draft(c);batch=self._input_batch(c)
            if d and batch and str(d['id'])==identity and d['version']==int(version) and any(x.get('draft_id')==identity for x in batch['items']):
                c.execute('UPDATE operation_drafts SET version=version+1 WHERE id=%s',(d['id'],))
                return self._finance_prompt(c,self._draft(c))
        if callback=='tqueue':
            batch=self._input_batch(c)
            if batch and self._capture_enabled(c):return Reply('Продолжаю…',capture_batch_id=str(batch['id']))
            return self._batch_card(c,batch)
        action,_,raw=callback.partition(':')
        if action not in ('titem','tdrop'):return None
        identity,_,index=raw.partition(':');index=int(index)
        batch=self._input_batch(c)
        if not batch or batch['id']!=UUID(identity) or not 0<=index<len(batch['items']):return Reply('Список недоступен или завершён.')
        item=batch['items'][index]
        if item.get('excluded'):return Reply('Строка уже исключена.')
        if action=='tdrop':
            if item.get('draft_id'):
                saved=c.execute('SELECT state FROM operation_drafts WHERE id=%s',(UUID(item['draft_id']),)).fetchone()
                if saved and saved['state']=='saved':return Reply('Строка уже сохранена. Отмена записи доступна через /history.')
                c.execute("UPDATE operation_drafts SET state='cancelled' WHERE id=%s AND state='pending'",(UUID(item['draft_id']),))
            item['excluded']=True
            c.execute('UPDATE input_batches SET items=%s WHERE id=%s',(Jsonb(batch['items']),batch['id']))
            return self._batch_card(c,batch)
        if item.get('draft_id'):
            draft=c.execute('SELECT * FROM operation_drafts WHERE id=%s',(UUID(item['draft_id']),)).fetchone()
            return self._prompt(c,draft) if draft and draft['state']=='pending' else self._batch_card(c,batch)
        if self._draft(c):return Reply('Сначала завершите текущий черновик. /add — показать; /cancel — отменить.')
        row={'id':batch['workspace_id'],'account_id':batch['account_id'],'timezone':batch['timezone_snapshot']}
        reply=self._text_create_draft(c,user,batch['source_sent_at'],row,item)
        item['draft_id']=reply.capture_draft_id or str(self._draft(c)['id'])
        c.execute('UPDATE input_batches SET items=%s WHERE id=%s',(Jsonb(batch['items']),batch['id']))
        if not self._capture_enabled(c):reply.text+='\n/batch — остальные строки.'
        return reply

    def _input_command(self,c,user,command,arg,sent):
        if command=='/batch':
            batch=self._input_batch(c)
            if batch and arg=='cancel':
                for item in batch['items']:
                    if item.get('draft_id'):c.execute("UPDATE operation_drafts SET state='cancelled' WHERE id=%s AND state='pending'",(UUID(item['draft_id']),))
                c.execute("UPDATE input_batches SET state='cancelled' WHERE id=%s",(batch['id'],))
                return Reply('Оставшиеся строки отменены. Сохранённые операции не изменены.')
            return self._batch_card(c,batch)
        if command=='/categories':
            action,_,rest=arg.partition(' ')
            if action in ('add','rename','archive','restore'):
                name,_,new=rest.partition('|')
                try:
                    with c.transaction():c.execute('SELECT manage_category(%s,%s,%s)',(action,name.strip(),new.strip() or None))
                except RaiseException as exc:raise ValueError(exc.diag.message_primary) from None
            page=int(arg) if arg.isdigit() else 1
            if not 1<=page<=10000:raise ValueError('Номер страницы: от 1 до 10000.')
            return self._category_settings(c,page)
        if command=='/search' or (command=='/history' and '=' in arg):
            filters={};term='';page=1
            if command=='/search':
                term,_,tail=arg.partition('|');term=term.strip();page=int(tail.strip() or '1')
                if not 1<=len(term)<=100:raise ValueError('/search описание | номер страницы')
            else:
                for piece in arg.split('|'):
                    key,sep,value=piece.strip().partition('=')
                    if not sep or key not in ('type','from','to','category','account','page','source','merchant','min','max'):raise ValueError('/history type=income | from=2026-09-01 | to=2026-09-10 | account=Основной | category=Продукты | page=1')
                    filters[key]=value.strip()
                page=int(filters.get('page','1'))
            if not 1<=page<=100000:raise ValueError('Страница должна быть положительным числом.')
            clauses=["o.state='active'"];args=[]
            if term:clauses.append('position(lower(%s) in lower(r.description))>0');args.append(term)
            for key,col in [('type','o.kind'),('category','cat.name'),('account','a.name'),('source','r.source_kind'),('merchant','r.merchant')]:
                if filters.get(key):
                    value=filters[key]
                    if key=='type':value=next((k for k,v in KINDS.items() if v.lower()==value.lower()),value)
                    clauses.append(f'lower({col})=lower(%s)');args.append(value)
            for key,operator in [('from','>='),('to','<=')]:
                if filters.get(key):clauses.append('r.occurred_on'+operator+'%s');args.append(date.fromisoformat(filters[key]))
            for key,operator in [('min','>='),('max','<=')]:
                if filters.get(key):
                    from balans.domain import amount_from_text
                    value=Decimal(0) if filters[key]=='0' else amount_from_text(filters[key])
                    clauses.append('r.amount'+operator+'%s');args.append(value)
            rows=c.execute('SELECT o.id,o.kind,r.occurred_on,r.amount,r.currency,r.description,a.name AS account FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id JOIN accounts a ON a.id=r.account_id LEFT JOIN categories cat ON cat.id=r.category_id WHERE '+' AND '.join(clauses)+' ORDER BY r.occurred_on DESC,o.id LIMIT 6 OFFSET %s',(*args,(page-1)*5)).fetchall()
            return Reply(f'Найденные операции · страница {page}\n'+'\n\n'.join(f"{KINDS[r['kind']]} · {r['occurred_on']} · {money(r['amount'],r['currency'])}\n{r['description'][:150]} · {r['account']}" for r in rows[:5])+('\nЕсть следующая страница: увеличьте page или номер после |.' if len(rows)>5 else '')+('Нет операций.' if not rows else ''),[[('Изменить',f"fedit:{r['id']}"),('История изменений',f"faudit:{r['id']}")] for r in rows[:5]])
        return None
