"""Personal financial operations with explicit confirmation and immutable revisions."""
from uuid import UUID
from decimal import Decimal
from psycopg.errors import RaiseException
from balans.domain import Reply,amount_from_text,date_from_text,money,CURRENCY
from balans.operation_editor import OperationEditor

KINDS={'expense':'Расход','income':'Доход','transfer':'Перевод','refund':'Возврат','opening':'Начальный остаток'}


class Finance(OperationEditor):
    def _account_context(self,c):
        row=c.execute("SELECT w.id,a.currency,a.id AS account_id,a.name AS account_name,CASE WHEN w.kind='shared' THEN w.timezone ELSE s.timezone END AS timezone FROM workspaces w JOIN user_settings s ON s.user_id=actor_user_id() JOIN accounts a ON a.workspace_id=w.id JOIN memberships m ON m.id=a.responsible_membership_id WHERE w.id=current_workspace() AND m.user_id=actor_user_id() AND m.status='active' ORDER BY (a.id=s.default_account_id) DESC NULLS LAST,(a.name='Основной') DESC,a.id LIMIT 1").fetchone()
        if row:CURRENCY.set(row['currency'])
        return row

    def _account_currency(self,c,identity):
        row=c.execute('SELECT currency FROM accounts WHERE id=%s',(identity,)).fetchone()
        return row['currency'] if row else CURRENCY.get()

    def _account_name(self,c,identity):
        row=c.execute('SELECT name FROM accounts WHERE id=%s',(identity,)).fetchone()
        return row['name'] if row else 'Недоступен'

    def _save_operation(self,c,identity):
        try:
            with c.transaction():
                operation=c.execute('SELECT save_expense(%s) AS id',(identity,)).fetchone()['id']
                self._attach_source_documents(c,operation)
                return operation
        except RaiseException as exc:
            raise ValueError(exc.diag.message_primary) from None

    def _finance_new(self,c,user,sent,kind,value=None,description='',day=None,destination=None,refund=None):
        if self._document_upload(c):return Reply('Сначала завершите прикрепление документа или /cancel.')
        if self._media_queue(c):return self._media_blocker(c)
        if kind=='income' and c.execute("SELECT kind='shared' AS shared FROM workspaces WHERE id=current_workspace()").fetchone()['shared']:
            return Reply('Приход в совместном бюджете требует сверки: /claim сумма | дата | источник | назначение. Получение по выдаче: /funds.')
        if self._draft(c):return self._prompt(c,self._draft(c))
        if self._voice_busy(c) or c.execute("SELECT id FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone():
            return Reply('Сначала завершите обработку или отправьте /cancel.')
        row=self._account_context(c)
        if kind=='opening':
            existing=c.execute("SELECT o.id FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.kind='opening' AND o.state='active' AND r.account_id=%s AND o.created_by_user_id=%s",(row['account_id'],user)).fetchone()
            if existing:
                reply=self._finance_callback(c,user,'fedit:'+str(existing['id']),sent)
                reply.text='Начальный остаток уже задан. Можно исправить его здесь.\n\n'+reply.text
                return reply
        category=None
        if refund:
            original=c.execute("SELECT r.* FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.created_by_user_id=actor_user_id() AND o.id=%s AND o.kind='expense' AND o.state='active'",(refund,)).fetchone()
            if not original:return Reply('Исходная покупка недоступна.')
            category=original['category_id'];description='Возврат: '+original['description'][:450]
        negative=bool(value and value.startswith('-'))
        if negative and kind!='opening':raise ValueError('Сумма должна быть положительной.')
        amount=amount_from_text(value.lstrip('-')) if value else None
        c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,category_id,timezone_snapshot,source_sent_at,amount,description,occurred_on,step,kind,destination_account_id,refund_of,opening_negative) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (row['id'],user,row['account_id'],category,row['timezone'],sent,amount,description,date_from_text(day or 'сегодня',sent,row['timezone']),'confirm' if amount else 'amount',kind,destination,refund,negative))
        if kind in ('income','opening') and self._capture_enabled(c):
            c.execute('UPDATE operation_drafts SET automatic_capture=true WHERE id=%s',(self._draft(c)['id'],))
            if amount is not None:return self._prompt(c,self._draft(c))
        return self._finance_prompt(c,self._draft(c))

    def _editor_reply(self,d,reply):
        if d.get('edit_operation_id') and d.get('state')=='pending':reply.edit_draft_id=str(d['id'])
        return reply

    def _finance_prompt(self,c,d):
        CURRENCY.set(self._account_currency(c,d['account_id']))
        if d['finance_edit_field']:
            prompts={'amount':'Введите новую сумму.', 'date':'Введите новую дату: сегодня, вчера или ДД.ММ.ГГГГ.', 'description':'Введите новое описание, до 500 символов.', 'destination':'Введите точное название счёта получателя.', 'account':'Введите точное название счёта.', 'received':'Введите фактически зачисленную сумму в валюте счёта получателя.', 'reason':'Укажите причину отмены, до 500 символов.'}
            prompt=prompts[d['finance_edit_field']]
            if d['finance_edit_field']=='amount' and d['kind']=='opening':prompt+=' Для отрицательного начального остатка используйте минус.'
            return self._editor_reply(d,Reply(prompt,[[('Отмена',f"cancel:{d['id']}:{d['version']}")]]))
        if d['step']=='amount':return Reply('Введите сумму в валюте учёта '+CURRENCY.get()+'. /cancel — отмена.')
        suffix=f"{d['id']}:{d['version']}"
        text=(('Отмена операции' if d['cancel_operation'] else ('Исправление: ' if d['edit_operation_id'] else '')+KINDS[d['kind']])+f"\nСумма: {money(-d['amount'] if d['opening_negative'] else d['amount'])}\n"
              +(f"Получатель: {self._account_name(c,d['destination_account_id'])}\n" if d['destination_account_id'] else '')+f"Дата: {d['occurred_on']:%d.%m.%Y}\nОписание: {d['description'] or '—'}\n"
              +(f"Причина: {d['change_reason']}\n" if d['cancel_operation'] else ''))
        if not d['edit_operation_id'] and d['amount'] is not None and d['occurred_on'] is not None and c.execute("SELECT o.id FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.kind=%s AND o.state='active' AND r.amount=%s AND r.occurred_on=%s LIMIT 1",(d['kind'],d['amount'],d['occurred_on'])).fetchone():text+='\nВозможный дубль: есть операция того же типа, суммы и даты. Подтверждайте только отдельное движение денег.'
        if d['kind']=='transfer' and d.get('destination_amount') is not None:text+='\nЗачисление: '+money(d['destination_amount'],self._account_currency(c,d['destination_account_id']))
        if d['edit_operation_id'] and not d['cancel_operation']:
            category=c.execute('SELECT name FROM categories WHERE id=%s',(d['category_id'],)).fetchone()
            if category:text+='\n'+('⚠️ ' if category['name']=='Без категории' else '')+'Категория: '+category['name']
            if 'date' in d['capture_warnings']:text+='\n⚠️ Дата взята из сообщения — уточните при необходимости.'
            original=self._editor_original(c,d)
            buttons=[[(label,f'ffield:{field}:{suffix}')] for label,field in [('Изменить сумму','amount'),('Изменить дату','date'),('Изменить описание','description')]]
            if d['kind']=='expense':buttons.append([('Изменить категорию',f'recat:{suffix}')])
            if original and not self._editor_unchanged(d,original):buttons.append([('Сохранить изменения',f'fsave:{suffix}')])
            buttons.append([('Отмена',f'cancel:{suffix}')])
            return self._editor_reply(d,Reply(text,buttons))

        if d['edit_operation_id'] and d['cancel_operation']:
            return self._editor_reply(d,Reply(text,[[('Сохранить изменения',f'fsave:{suffix}')],[('Отмена',f'cancel:{suffix}')]]))

        buttons=[[('Подтвердить',f'fsave:{suffix}'),('Отмена',f'cancel:{suffix}')]]
        if not d['cancel_operation']:
            buttons.append([(label,f'ffield:{field}:{suffix}') for label,field in [('Сумма','amount'),('Дата','date'),('Описание','description')]])

        if d['kind']=='transfer' and not d['cancel_operation'] and self._account_currency(c,d['account_id'])!=self._account_currency(c,d['destination_account_id']):buttons.append([('Сумма зачисления',f'ffield:received:{suffix}')])
        return Reply(text,buttons)

    def _finance_text(self,c,d,text,message_id=None):
        field=d['finance_edit_field'] or ('amount' if d['step']=='amount' else None)
        if d.get('edit_operation_id') and not field and d['step']=='category':
            category=c.execute('SELECT id FROM categories WHERE NOT archived AND workspace_id=%s AND lower(name)=lower(%s)',(d['workspace_id'],text)).fetchone()
            if not category:
                reply=self._category_menu(c,d)
                reply.text='Категория не найдена.\n'+reply.text
                return self._editor_reply(d,reply)
            return self._select_category(c,d,category['id'],message_id)
        if not field:return self._finance_prompt(c,d)
        if d.get('edit_operation_id'):self._remember_editor_message(c,d,message_id)
        if field=='received':
            if d['kind']!='transfer':raise ValueError('Поле доступно только для перевода.')
            c.execute('UPDATE operation_drafts SET destination_amount=%s WHERE id=%s',(amount_from_text(text),d['id']))
        elif field=='amount':
            negative=text.startswith('-')
            if negative and d['kind']!='opening':raise ValueError('Сумма должна быть положительной.')
            c.execute('UPDATE operation_drafts SET amount=%s,opening_negative=%s WHERE id=%s',(amount_from_text(text[1:] if negative else text),negative,d['id']))
        elif field=='date':c.execute('UPDATE operation_drafts SET occurred_on=%s WHERE id=%s',(date_from_text(text,d['source_sent_at'],d['timezone_snapshot']),d['id']))
        elif field in ('account','destination'):
            account=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.user_id=actor_user_id() AND lower(a.name)=lower(%s)',(text,)).fetchone()
            if not account:raise ValueError('Счёт не найден. /accounts — список.')
            if field=='account':c.execute('UPDATE operation_drafts SET account_id=%s WHERE id=%s',(account['id'],d['id']))
            else:c.execute('UPDATE operation_drafts SET destination_account_id=%s WHERE id=%s',(account['id'],d['id']))
        else:
            if not text or len(text)>500:raise ValueError('Введите от 1 до 500 символов.')
            if field=='reason':c.execute('UPDATE operation_drafts SET change_reason=%s WHERE id=%s',(text,d['id']))
            else:c.execute('UPDATE operation_drafts SET description=%s WHERE id=%s',(text,d['id']))
        if field in ('date','description'):
            c.execute('UPDATE operation_drafts SET capture_warnings=array_remove(capture_warnings,%s) WHERE id=%s',(field,d['id']))
        current=self._draft(c)
        if current['kind']=='transfer' and self._account_currency(c,current['account_id'])==self._account_currency(c,current['destination_account_id']):c.execute('UPDATE operation_drafts SET destination_amount=amount WHERE id=%s',(d['id'],))
        c.execute("UPDATE operation_drafts SET step='confirm',finance_edit_field=NULL,version=version+1 WHERE id=%s",(d['id'],))
        current=self._draft(c)
        if current.get('edit_operation_id') and not current.get('cancel_operation'):
            return self._finish_editor_change(c,current,message_id)
        return self._prompt(c,self._draft(c)) if d['automatic_capture'] and not d['edit_operation_id'] else self._finance_prompt(c,self._draft(c))

    def _finance_callback(self,c,user,callback,sent,message_id=None):
        editor=self._editor_action(c,user,callback,sent,message_id)
        if editor is not None:return editor
        action,_,raw=callback.partition(':')
        if action=='hback':
            identity,version=raw.split(':')
            draft=c.execute("SELECT * FROM operation_drafts WHERE id=%s AND author_user_id=%s AND edit_operation_id IS NOT NULL FOR UPDATE",(UUID(identity),user)).fetchone()
            updated=c.execute("UPDATE operation_drafts SET state='cancelled',version=version+1 WHERE id=%s AND version=%s AND author_user_id=%s AND state='pending' AND edit_operation_id IS NOT NULL",(UUID(identity),int(version),user))
            if not updated.rowcount:return Reply('Эта форма редактирования уже закрыта или устарела. Операция не изменена.',[[('Открыть историю','history')]])
            origin=c.execute("SELECT payload FROM fund_confirmations WHERE author_user_id=%s AND payload->>'action'='history_edit' AND payload->>'draft'=%s LIMIT 1",(user,identity)).fetchone()
            reply=self._history_page(c,origin['payload']['page'] if origin else 1)
            reply.delete_message_ids=self._editor_cleanup_ids(draft,message_id,include_origin=False)
            return reply
        if action=='acuse':
            account=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.user_id=actor_user_id() AND a.id=%s',(UUID(raw),)).fetchone()
            if not account:return Reply('Счёт недоступен.')
            c.execute('UPDATE user_settings SET default_account_id=%s WHERE user_id=%s',(account['id'],user))
            return Reply('Счёт для новых операций: '+self._account_name(c,account['id'])+'. Открытый черновик сохраняет прежний счёт.')
        if action in ('fedit','fdelete','frefund','faudit'):
            o=c.execute('SELECT o.*,r.* ,o.id AS operation_id FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=%s AND (o.created_by_user_id=%s OR %s)',(UUID(raw),user,action=='faudit')).fetchone()
            if not o:return Reply('Операция недоступна.')
            if action=='faudit':
                rows=c.execute('SELECT revision_no,operation_kind,opening_negative,amount,currency,description,occurred_on,change_reason FROM operation_revisions WHERE operation_id=%s ORDER BY revision_no DESC LIMIT 10',(o['operation_id'],)).fetchall()
                return Reply('История изменений (последние 10)\n'+'\n\n'.join(f"Версия {r['revision_no']} · {KINDS[r['operation_kind']]} · {r['occurred_on']} · {money(-r['amount'] if r['operation_kind']=='opening' and r['opening_negative'] else r['amount'],r['currency'])}\n{r['description'][:100]}\n{r['change_reason'] or '—'}" for r in rows))
            if o['state']!='active':return Reply('Операция уже отменена.')
            if action=='frefund':return self._finance_new(c,user,sent,'refund',refund=o['operation_id'])
            if self._draft(c):return Reply('Сначала завершите текущий черновик или /cancel.')
            c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,category_id,amount,description,occurred_on,timezone_snapshot,source_sent_at,step,kind,destination_account_id,refund_of,edit_operation_id,expected_revision_id,cancel_operation,opening_negative,finance_edit_field,change_reason,destination_amount) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirm',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (o['workspace_id'],user,o['account_id'],o['category_id'],o['amount'],o['description'],o['occurred_on'],o['timezone_snapshot'],sent,o['kind'],o['destination_account_id'],o['refund_of'],o['operation_id'],o['current_revision_id'],action=='fdelete',o['opening_negative'],'reason' if action=='fdelete' else None,'Исправление пользователем',o['destination_amount']))
            c.execute('UPDATE operation_drafts SET capture_warnings=%s WHERE id=%s',(o['capture_warnings'],self._draft(c)['id']))
            if message_id:
                c.execute('UPDATE operation_drafts SET edit_origin_message_id=%s WHERE id=%s',(int(message_id),self._draft(c)['id']))
            return self._finance_prompt(c,self._draft(c))
        if action not in ('fsave','ffield'):return None
        field=None
        if action=='ffield':field,_,raw=raw.partition(':')
        identity,_,version=raw.partition(':')
        d=c.execute("SELECT * FROM operation_drafts WHERE id=%s AND version=%s",(UUID(identity),int(version))).fetchone()
        if not d or d['state']=='cancelled':
            return Reply('Эта форма редактирования уже закрыта или устарела. Операция не изменена.',[[('Открыть историю','history')]])
        if d['state']=='saved':return Reply('Изменения уже сохранены. Повторной записи нет.')
        if d['edit_operation_id']:self._remember_editor_message(c,d,message_id)
        if action=='ffield':
            if field not in ('amount','date','description','account','destination','received'):return Reply('Поле недоступно.')
            c.execute('UPDATE operation_drafts SET finance_edit_field=%s,version=version+1 WHERE id=%s',(field,d['id']))
            return self._finance_prompt(c,self._draft(c))
        if d['edit_operation_id'] and not d['cancel_operation']:
            original=self._editor_original(c,d)
            if original and self._editor_unchanged(d,original):return self._finance_prompt(c,d)
        operation=self._save_operation(c,d['id'])
        if d['edit_operation_id']:
            delete_ids=self._editor_cleanup_ids(d,message_id,include_origin=True)
            if not d['cancel_operation']:
                reply=self._capture_card(c,operation)
            else:
                reply=Reply('Операция удалена.',[[('История','history'),('Меню','ui:menu')]])
            reply.delete_message_ids=delete_ids
            return reply
        return Reply('Операция отменена.' if d['cancel_operation'] else 'Операция сохранена.',[[('История','history'),('Меню','ui:menu')]])

    def _finance_command(self,c,user,command,arg,sent):
        if command=='/accounts':
            default=self._account_context(c)['account_id']
            zone=self._account_context(c)['timezone']
            rows=c.execute("SELECT a.id,a.name,a.currency,m.user_id=actor_user_id() AS selectable,coalesce((SELECT sum(p.delta) FROM postings p JOIN journal_entries e ON e.id=p.entry_id WHERE p.account_id=a.id AND e.effective_on<=(now() AT TIME ZONE %s)::date),0)+coalesce((SELECT sum(f.delta) FROM fund_entries f WHERE f.account_id=a.id AND f.effective_on<=(now() AT TIME ZONE %s)::date),0) AS balance,coalesce((SELECT sum(cl.amount) FROM fund_claims cl WHERE cl.account_id=a.id AND cl.state='pending'),0) AS unverified FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id ORDER BY a.name,a.id",(zone,zone)).fetchall()
            shared=c.execute("SELECT kind='shared' AS shared FROM workspaces WHERE id=current_workspace()").fetchone()['shared']
            text='Счета · по валютам\n'+'\n'.join(('✓ ' if r['id']==default else '')+r['name']+': '+money(r['balance'],r['currency'])+((' · сверенный; заявленный: '+money(r['balance']+r['unverified'],r['currency'])) if shared else '')+(' · отрицательный остаток: проверьте пропущенные приходы' if r['balance']<0 else '') for r in rows)
            return Reply(text+'\nЭто учётные остатки.\n/account Название — создать счёт\n/opening 10000 | сегодня — начальный остаток\nНажмите свой счёт для новых операций.',[[(r['name'],f"acuse:{r['id']}")] for r in rows if r['selectable']])
        if command=='/account':
            if not arg or len(arg)>80:raise ValueError('/account Название — от 1 до 80 символов.')
            identity=self._workspace_call(c,'SELECT create_account(%s) AS id',(arg,))['id']
            return Reply('Счёт создан: '+self._account_name(c,identity)+'. Начальный остаток — 0.',[[('Использовать этот счёт',f'acuse:{identity}')]])
        if command in ('/income','/opening','/transfer'):
            if len(arg)>700:raise ValueError('Слишком длинная запись.')
            pieces=[x.strip() for x in arg.split('|')]
            if len(pieces)>4 or any(len(x)>500 for x in pieces):raise ValueError('Описание: до 500 символов; используйте формат из /help.')
            if command=='/transfer':
                if len(pieces)<2:return Reply('/transfer 1000 | Наличные | сегодня | назначение\nСписание — со счёта, выбранного в /accounts.')
                target=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.user_id=actor_user_id() AND lower(a.name)=lower(%s)',(pieces[1],)).fetchone()
                if not target:raise ValueError('Счёт получателя не найден. Создайте его: /account Название')
                return self._finance_new(c,user,sent,'transfer',pieces[0],pieces[3] if len(pieces)>3 else 'Перевод между своими счетами',pieces[2] if len(pieces)>2 else None,target['id'])
            if command=='/opening':return self._finance_new(c,user,sent,'opening',pieces[0] or None,'Начальный остаток',pieces[1] if len(pieces)>1 else None)
            return self._finance_new(c,user,sent,'income',pieces[0] or None,pieces[1] if len(pieces)>1 else 'Доход',pieces[2] if len(pieces)>2 else None)
        return None
