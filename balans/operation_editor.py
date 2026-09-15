"""Actions available only after opening a saved operation for editing."""
from uuid import UUID
from decimal import Decimal
from psycopg.types.json import Jsonb
from balans.domain import Reply, money


class OperationEditor:
    def _remember_editor_message(self,c,d,message_id):
        if not d or not d.get('edit_operation_id') or message_id is None:return
        c.execute("""UPDATE operation_drafts
                    SET edit_message_ids=CASE
                      WHEN %s=ANY(edit_message_ids) THEN edit_message_ids
                      ELSE array_append(edit_message_ids,%s)
                    END
                    WHERE id=%s AND state='pending' AND edit_operation_id IS NOT NULL""",
                  (int(message_id),int(message_id),d['id']))

    def _editor_cleanup_ids(self,d,current_message_id=None,include_origin=True):
        if not d:return []
        ids=[]
        if include_origin and d.get('edit_origin_message_id') is not None:
            ids.append(int(d['edit_origin_message_id']))
        ids.extend(int(value) for value in (d.get('edit_message_ids') or []))
        if current_message_id is not None:ids.append(int(current_message_id))
        return list(dict.fromkeys(ids))

    def _finish_editor_change(self,c,d,message_id=None):
        """Save one submitted field and update the original Telegram card in place."""
        if not d or not d.get('edit_operation_id') or d.get('cancel_operation'):
            return None
        try:
            operation=self._save_operation(c,d['id'])
        except ValueError as exc:
            reply=self._finance_prompt(c,d)
            reply.text='Изменение пока не сохранено: '+str(exc)+'\n\n'+reply.text
            return self._editor_reply(d,reply)
        reply=self._capture_card(c,operation)
        reply.delete_message_ids=self._editor_cleanup_ids(d,message_id,include_origin=False)
        if d.get('edit_origin_message_id') is not None:
            reply.edit_message_id=int(d['edit_origin_message_id'])
        return reply

    def _editor_original(self,c,d):
        return c.execute('SELECT o.kind,o.state,o.current_revision_id,r.* FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=%s AND o.created_by_user_id=actor_user_id()',(d['edit_operation_id'],)).fetchone()

    def _editor_unchanged(self,d,original):
        return all(d[key]==original[key] for key in ('kind','amount','occurred_on','description','category_id','opening_negative','account_id','destination_account_id','destination_amount'))

    def _editor_source(self,c,d):
        return c.execute('SELECT b.* FROM operations o JOIN operation_drafts source ON source.id=o.source_draft_id JOIN receipt_batches b ON b.id=source.receipt_batch_id WHERE o.id=%s',(d['edit_operation_id'],)).fetchone()

    def _editor_split_items(self,c,d):
        batch=self._editor_source(c,d)
        if not batch:return Reply('У записи нет распознанных позиций чека.')
        result=batch['result'] or {}
        # Media operations keep their line items on a child batch.
        rows=c.execute('SELECT name,quantity,unit_price,line_total,discount FROM receipt_items WHERE batch_id=%s ORDER BY line_no',(batch['id'],)).fetchall()
        if result.get('items'):rows=result['items']
        complete=result.get('items_complete',False)
        if batch['parent_batch_id']:
            q=c.execute("SELECT item FROM media_queues q, jsonb_array_elements(q.items) item WHERE item->>'result_id'=%s ORDER BY q.created_at LIMIT 1",(str(d['edit_operation_id']),)).fetchone()
            if q:
                complete=q['item'].get('items_complete',False)
                rows=q['item'].get('items',rows)
        source=c.execute('SELECT r.source_kind FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=%s',(d['edit_operation_id'],)).fetchone()
        parent=dict(kind='expense',amount=str(d['amount']),currency=self._account_currency(c,d['account_id']),
                    occurred_on=d['occurred_on'].isoformat(),merchant=d['merchant'] or '',description=d['description'],
                    category_id=str(d['category_id']) if d['category_id'] else None,payment_status='paid',
                    items=rows,items_complete=complete,source_type=source['source_kind'] if source['source_kind'] in ('receipt','screenshot','terminal') else 'receipt',split_group=str(batch['parent_batch_id'] or batch['id']))
        items=self._split_lines(c,parent,d['workspace_id'])
        if isinstance(items,Reply):return items
        for item in items:
            if not item['category_id']:item['category_id']=str(c.execute('SELECT uncategorized_category(%s) AS id',(d['workspace_id'],)).fetchone()['id'])
            item.update(state='pending',account=str(d['account_id']),destination=None,refund=None,paid=True,
                        duplicate_confirmed=True,split_parent_id=str(d['edit_operation_id']))
        return batch,items

    def _editor_action(self,c,user,callback,sent,message_id=None):
        action,_,raw=callback.partition(':')
        if action not in ('faction','ftype'):return None
        value,identity,version=raw.split(':')
        d=c.execute("SELECT * FROM operation_drafts WHERE id=%s AND author_user_id=%s AND version=%s AND state='pending' AND expires_at>now() AND edit_operation_id IS NOT NULL FOR UPDATE",(UUID(identity),user,int(version))).fetchone()
        if not d:return Reply('Эта форма редактирования уже закрыта или устарела. Операция не изменена.',[[('Открыть историю','history')]])
        self._remember_editor_message(c,d,message_id)
        original=self._editor_original(c,d)
        if not original or original['state']!='active' or original['current_revision_id']!=d['expected_revision_id']:
            return Reply('Эта форма редактирования уже закрыта или устарела. Операция не изменена.',[[('Открыть историю','history')]])
        suffix=f"{d['id']}:{d['version']}"
        if d['cancel_operation']:return self._finance_prompt(c,d)
        if value=='type' or action=='ftype':
            personal=c.execute("SELECT kind='personal' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']
            if not personal or original['kind'] not in ('expense','income','opening'):
                return Reply('Тип этой связанной операции изменить нельзя.',[[('К записи',f'ui:resume')]])
            if action=='faction':
                return Reply('Выберите тип операции. Баланс и отчёты изменятся после сохранения.',[
                    [(label,f'ftype:{kind}:{suffix}')] for kind,label in [('expense','Расход'),('income','Доход'),('opening','Начальный остаток')]]+[[('Назад','ui:resume')]])
            if value not in ('expense','income','opening'):return Reply('Тип недоступен.')
            category=d['category_id'] if value=='expense' else None
            if value=='expense' and not category:category=c.execute('SELECT uncategorized_category(%s) AS id',(d['workspace_id'],)).fetchone()['id']
            c.execute("UPDATE operation_drafts SET kind=%s,category_id=%s,opening_negative=CASE WHEN %s='opening' THEN opening_negative ELSE false END,finance_edit_field=NULL,step='confirm',version=version+1 WHERE id=%s",(value,category,value,d['id']))
            return self._finance_prompt(c,self._draft(c))
        if value=='delete':
            c.execute("UPDATE operation_drafts SET kind=%s,amount=%s,occurred_on=%s,description=%s,category_id=%s,opening_negative=%s,cancel_operation=true,change_reason='Удалено пользователем',finance_edit_field=NULL,step='confirm',version=version+1 WHERE id=%s",(original['kind'],original['amount'],original['occurred_on'],original['description'],original['category_id'],original['opening_negative'],d['id']))
            reply=self._finance_prompt(c,self._draft(c))
            reply.text='Удалить только эту операцию? Баланс будет пересчитан.\n\n'+reply.text
            return reply
        if value not in ('attach','documents','split','splitok'):return Reply('Действие недоступно.')
        if not self._editor_unchanged(d,original):
            return Reply('Сначала сохраните изменения записи.',[[('Вернуться к изменениям','ui:resume')]])
        if value in ('attach','documents'):
            if self._media_queue(c) or self._voice_busy(c):return Reply('Сначала завершите обработку файла.',[[('Открыть текущий ввод','ui:resume')]])
            c.execute("UPDATE operation_drafts SET state='cancelled',version=version+1 WHERE id=%s",(d['id'],))
            if value=='documents':return self._document_card(c,self._document_set(c,'operation',d['edit_operation_id']))
            reply=self._begin_document_upload(c,'operation',d['edit_operation_id'])
            if self._document_upload(c):
                reply=Reply('📎 Пришлите чек или документ для «'+d['description']+'».\nJPEG, PNG или PDF до 15 МБ. Новая операция не создаётся.',[[('Отмена','ui:go:cancel')]])
            return reply
        if original['kind']!='expense':return Reply('Разбиение доступно только для расхода.')
        if self._media_queue(c) or self._input_batch(c):return Reply('Сначала завершите текущий список.',[[('Открыть список','ui:resume')]])
        split=self._editor_split_items(c,d)
        if isinstance(split,Reply):
            split.buttons=[[('Назад','ui:resume')]]
            return split
        batch,items=split
        if value=='split':
            categories={str(x['id']):x['name'] for x in self._categories(c,d['workspace_id'])}
            lines=[f"{i}. {x['description']} — {money(Decimal(x['amount']),x['currency'])} · {categories.get(x['category_id'],'Без категории')}" for i,x in enumerate(items,1)]
            text='Заменить общий расход позициями чека?\n\n'+'\n'.join(lines)+'\n\nИтог: '+money(d['amount'],self._account_currency(c,d['account_id']))+'. Баланс не изменится. Категорию каждой позиции можно исправить после сохранения.'
            if len(text)>3500:return Reply('В чеке слишком много позиций для разбиения одной карточкой.',[[('Назад','ui:resume')]])
            return Reply(text,[[('Подтвердить разбиение',f'faction:splitok:{suffix}')],[('Отмена','ui:resume')]])
        # Reverse the parent and save every part in one transaction. A failure
        # rolls back the reversal and all children, keeping the original expense.
        with c.transaction():
            c.execute("UPDATE operation_drafts SET cancel_operation=true,change_reason='Разбиение чека',finance_edit_field=NULL,step='confirm' WHERE id=%s",(d['id'],))
            self._save_operation(c,d['id'])
            q=c.execute('INSERT INTO media_queues(workspace_id,author_user_id,source_batch_id,items,split_operation_id) VALUES(%s,%s,%s,%s,%s) RETURNING *',(d['workspace_id'],user,batch['parent_batch_id'] or batch['id'],Jsonb(items),d['edit_operation_id'])).fetchone()
            cards=[]
            for i in range(len(items)):
                self._media_save(c,q,i)
                q=c.execute('SELECT * FROM media_queues WHERE id=%s',(q['id'],)).fetchone()
                cards.append(self._capture_card(c,UUID(q['items'][i]['result_id'])))
            c.execute("UPDATE media_queues SET state='done' WHERE id=%s",(q['id'],))
        return self._capture_combine(cards)
