"""Save recognized facts once; uncertain optional fields remain visibly flagged."""
from dataclasses import asdict
from uuid import UUID
from zoneinfo import ZoneInfo
from balans.domain import Reply,money
from psycopg.types.json import Jsonb


class AutomaticCapture:
    def _capture_replay_card(self,c,identity):
        rows=c.execute("WITH RECURSIVE family(id) AS (SELECT id FROM operations WHERE id=%s UNION ALL SELECT o.id FROM family p JOIN operation_drafts d ON d.split_parent_id=p.id JOIN operations o ON o.source_draft_id=d.id) SELECT o.id FROM family f JOIN operations o ON o.id=f.id WHERE o.state='active' ORDER BY o.created_at,o.id",(identity,)).fetchall()
        if not rows:return None
        reply=self._capture_combine([self._capture_card(c,r['id']) for r in rows])
        reply.text='Этот расход уже записан.\n\n'+reply.text
        return reply

    def _capture_duplicates(self,c,d,day):
        source=d['source_type'] or ('receipt' if d['receipt_batch_id'] else 'voice' if d['voice_job_id'] else 'manual')
        return c.execute("SELECT o.id,r.description FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.state='active' AND o.created_by_user_id=actor_user_id() AND o.kind=%s AND r.currency=%s AND ((r.amount=%s AND r.occurred_on=%s) OR (%s::text IS NOT NULL AND r.external_reference_hash=%s)) AND (%s OR r.source_kind<>%s) ORDER BY o.created_at DESC LIMIT 3",(d['kind'],self._account_currency(c,d['account_id']),d['amount'],day,d['external_reference_hash'],d['external_reference_hash'],bool(d['receipt_batch_id']),source)).fetchall()

    def _capture_duplicate_callback(self,c,callback):
        if callback.startswith('capdup:'):
            _,identity,version=callback.split(':')
            d=c.execute("UPDATE operation_drafts SET duplicate_confirmed=true,version=version+1 WHERE id=%s AND version=%s AND state='pending' AND expires_at>now() AND author_user_id=actor_user_id() RETURNING *",(UUID(identity),int(version))).fetchone()
            return self._prompt(c,d) if d else Reply('Карточка устарела. Откройте актуальную запись из истории.',[[('Открыть историю','history')]])
        if not callback.startswith('caplink:'):return None
        token=c.execute("SELECT * FROM fund_confirmations WHERE id=%s AND state='pending' AND expires_at>now()",(UUID(callback.split(':')[1]),)).fetchone()
        if not token or token['payload'].get('action')!='capture_link':return Reply('Подтверждение устарело.')
        p=token['payload']
        d=c.execute("SELECT * FROM operation_drafts WHERE id=%s AND version=%s AND state='pending' AND expires_at>now() AND author_user_id=actor_user_id()",(UUID(p['draft']),p['version'])).fetchone()
        if not d or not d['receipt_batch_id']:return Reply('Карточка устарела.')
        day=d['occurred_on'] or d['source_sent_at'].astimezone(ZoneInfo(d['timezone_snapshot'])).date()
        if UUID(p['operation']) not in [r['id'] for r in self._capture_duplicates(c,d,day)]:return Reply('Запись изменилась. Повторите сопоставление.')
        ds=self._document_set(c,'operation',UUID(p['operation']))
        batch=c.execute('SELECT coalesce(parent_batch_id,id) AS id FROM receipt_batches WHERE id=%s',(d['receipt_batch_id'],)).fetchone()
        for f in c.execute('SELECT * FROM receipt_files WHERE batch_id=%s',(batch['id'],)).fetchall():
            self._store_document(c,ds,self.receipt_storage.read(f['id']),f['mime_type'],receipt=f['id'])
        c.execute("UPDATE operation_drafts SET state='cancelled',result_operation_id=%s WHERE id=%s",(UUID(p['operation']),d['id']))
        c.execute("UPDATE fund_confirmations SET state='done' WHERE id=%s",(token['id'],))
        reply=self._capture_card(c,UUID(p['operation']));reply.text='📎 Чек прикреплён. Новая операция не создана.\n\n'+reply.text
        return reply

    def _capture_enabled(self,c):
        return c.execute('SELECT automatic_capture FROM user_settings WHERE user_id=actor_user_id()').fetchone()['automatic_capture']

    def _capture_card(self,c,identity):
        row=c.execute('SELECT o.kind,r.*,cat.name AS category FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id LEFT JOIN categories cat ON cat.id=r.category_id WHERE o.id=%s',(identity,)).fetchone()
        if not row:return Reply('Запись недоступна.')
        warnings=set(row['capture_warnings']);category=row['category'] or 'Без категории'
        title={'income':'Доход записан','opening':'Начальный остаток записан','refund':'Возврат записан','transfer':'Перевод записан'}.get(row['kind'],'Расход записан')
        lines=['✅ '+title,'',('⚠️ ' if 'description' in warnings else '📝 ')+(row['description'] or 'Без описания'),'💰 '+money(-row['amount'] if row['kind']=='opening' and row['opening_negative'] else row['amount'],row['currency'])]
        if row['kind']=='expense':lines.append(('⚠️ ' if category=='Без категории' else '🏷 ')+category)
        lines.append(('⚠️ ' if 'date' in warnings else '📅 ')+row['occurred_on'].strftime('%d.%m.%Y')+(' · дата сообщения' if 'date' in warnings else ''))
        return Reply('\n'.join(lines),[[('✏️ Изменить',f'fedit:{identity}')]],command_hints=False)


    def _capture_draft(self,c,d):
        if not d or d['edit_operation_id'] or d['state']!='pending' or d['step']=='ai_pending':return None
        if d['kind'] not in ('expense','income','opening'):return None
        if d['capture_kind_pending']:
            suffix=f"{d['id']}:{d['version']}"
            return Reply('Это новый доход или деньги, которые уже были до начала учёта?',[[('Доход','incoming:income:'+suffix),('Начальный остаток','incoming:opening:'+suffix)]])
        if not d['automatic_capture']:return None
        if d['amount'] is None:
            if d['voice_job_id']:c.execute("UPDATE operation_drafts SET voice_edit_field='amount' WHERE id=%s",(d['id'],))
            elif d['receipt_batch_id']:c.execute("UPDATE operation_drafts SET receipt_edit_field='amount' WHERE id=%s",(d['id'],))
            else:c.execute("UPDATE operation_drafts SET step='amount' WHERE id=%s",(d['id'],))
            return Reply('⚠️ Не удалось определить сумму. Укажите сумму операции.',[[('Отмена',f"cancel:{d['id']}:{d['version']}")]])
        if d['receipt_batch_id']:
            result=c.execute('SELECT result FROM receipt_batches WHERE id=%s',(d['receipt_batch_id'],)).fetchone()['result'] or {}
            if not d['payment_confirmed'] and result.get('payment_status')!='paid':
                return Reply('⚠️ Не удалось определить, совершена ли покупка. Она уже оплачена?',[[('Да, оплачена',f"rpaid:{d['id']}:{d['version']}")],[('Не записывать',f"cancel:{d['id']}:{d['version']}")]])
            if d['receipt_currency'] not in (None,self._account_currency(c,d['account_id'])):return None
        if d['voice_edit_field'] or d['receipt_edit_field'] or d['finance_edit_field']:return None
        warnings=set(d['capture_warnings'])
        # Receipt/image ingestion already falls back to the upload date. Keep
        # the fallback warning for text and voice, where it still explains an
        # uncertain date to the user.
        if not d['occurred_on'] and not d['receipt_batch_id']:warnings.add('date')
        if not d['description']:warnings.add('description')
        category=d['category_id']
        if d['kind']=='expense' and not category:category=c.execute('SELECT uncategorized_category(%s) AS id',(d['workspace_id'],)).fetchone()['id']
        day=d['occurred_on'] or d['source_sent_at'].astimezone(ZoneInfo(d['timezone_snapshot'])).date()
        # Exact image replay is distinct from two legitimate purchases of the same amount.
        if d['receipt_batch_id']:
            duplicate=c.execute("SELECT o.id FROM operations o JOIN operation_drafts prior ON prior.id=o.source_draft_id JOIN receipt_batches pb ON pb.id=prior.receipt_batch_id JOIN receipt_files old ON old.batch_id=coalesce(pb.parent_batch_id,pb.id) JOIN receipt_files fresh ON fresh.sha256=old.sha256 WHERE fresh.batch_id=%s AND o.state='active' AND o.created_by_user_id=actor_user_id() AND prior.id<>%s LIMIT 1",(d['receipt_batch_id'],d['id'])).fetchone()
            if duplicate:
                c.execute("UPDATE operation_drafts SET state='cancelled',result_operation_id=%s WHERE id=%s",(duplicate['id'],d['id']))
                parent=c.execute('SELECT d.split_parent_id FROM operations o JOIN operation_drafts d ON d.id=o.source_draft_id WHERE o.id=%s',(duplicate['id'],)).fetchone()['split_parent_id']
                return self._capture_replay_card(c,parent or duplicate['id'])
        duplicates=[] if d['duplicate_confirmed'] else self._capture_duplicates(c,d,day)
        if duplicates:
            buttons=[[('Это другая операция',f"capdup:{d['id']}:{d['version']}")]]
            if d['receipt_batch_id']:
                for candidate in duplicates:
                    token=c.execute('INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s) RETURNING id',(Jsonb({'action':'capture_link','draft':str(d['id']),'version':d['version'],'operation':str(candidate['id'])}),)).fetchone()['id']
                    buttons.append([('Прикрепить к «'+candidate['description'][:30]+'»',f'caplink:{token}')])
            buttons.append([('Не добавлять',f"cancel:{d['id']}:{d['version']}")])
            return Reply('Возможный дубль: уже есть операция с такой суммой и датой или реквизитами. Сохранить отдельную запись?',buttons)
        c.execute("UPDATE operation_drafts SET category_id=%s,description=%s,occurred_on=%s,capture_warnings=%s,step='confirm',payment_confirmed=true,duplicate_confirmed=true,receipt_currency=CASE WHEN receipt_batch_id IS NOT NULL THEN %s ELSE receipt_currency END WHERE id=%s",(category,d['description'] or 'Без описания',day,sorted(warnings),self._account_currency(c,d['account_id']),d['id']))
        try:
            identity=self._save_operation(c,d['id'])
            c.execute("UPDATE input_batches b SET state='done' WHERE state='active' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(b.items) item LEFT JOIN operation_drafts pending ON pending.id=(item->>'draft_id')::uuid WHERE pending.id IS NULL OR pending.state='pending')")
            return self._capture_card(c,identity)
        except ValueError as exc:
            return Reply('⚠️ Запись пока не сохранена. '+str(exc),[[('Открыть текущую запись','ui:resume')],[('Отмена',f"cancel:{d['id']}:{d['version']}")]])

    def _capture_media_item(self,c,q,index):
        if not self._capture_enabled(c):return None
        item=q['items'][index]
        if item['state']!='pending':return None
        if item['kind'] not in ('expense','income','opening') or not item.get('source_type') or item['source_type']=='unknown':return None
        if item['kind']=='income' and c.execute("SELECT kind='shared' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']:return None
        if not item['amount'] or not (item['paid'] or item['payment_status']=='paid') or item['currency']!=self._account_currency(c,UUID(item['account'])):return None
        old=c.execute("SELECT items FROM media_queues prior WHERE prior.id<>%s AND prior.author_user_id=actor_user_id() AND prior.workspace_id=current_workspace() AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(prior.items) item WHERE item ? 'split_parent_id') AND EXISTS(SELECT 1 FROM jsonb_array_elements(prior.items) item WHERE item->>'result_kind'='operation') AND ARRAY(SELECT sha256 FROM receipt_files WHERE batch_id=prior.source_batch_id ORDER BY sha256)=ARRAY(SELECT sha256 FROM receipt_files WHERE batch_id=%s ORDER BY sha256) AND EXISTS(SELECT 1 FROM receipt_files WHERE batch_id=%s) ORDER BY prior.created_at DESC LIMIT 1",(q['id'],q['source_batch_id'],q['source_batch_id'])).fetchone()
        if old and index<len(old['items']) and old['items'][index].get('result_kind')=='operation' and old['items'][index].get('result_id'):
            identity=UUID(old['items'][index]['result_id'])
            reply=self._capture_replay_card(c,identity)
            if reply:
                item.update(state='saved',result_id=str(identity),result_kind='operation')
                c.execute('UPDATE media_queues SET items=%s,version=version+1 WHERE id=%s',(Jsonb(q['items']),q['id']))
                return reply
        if not item.get('duplicate_confirmed') and self._media_duplicates(c,item):return None
        warnings=[]
        if not item['occurred_on']:
            batch=c.execute('SELECT source_sent_at,timezone_snapshot FROM receipt_batches WHERE id=%s',(q['source_batch_id'],)).fetchone()
            item['occurred_on']=batch['source_sent_at'].astimezone(ZoneInfo(batch['timezone_snapshot'])).date().isoformat()
        if not item['description'] and not item['merchant']:item['description']='Без описания';warnings.append('description')
        if item['kind']=='expense' and not item['category_id']:item['category_id']=str(c.execute('SELECT uncategorized_category(%s) AS id',(q['workspace_id'],)).fetchone()['id'])
        item.update(paid=True,duplicate_confirmed=True,capture_warnings=warnings)
        try:
            return self._media_save(c,q,index)
        except ValueError as exc:
            return Reply('⚠️ Запись пока не сохранена. '+str(exc),[[('Открыть запись',f"mreview:{q['id']}:{index}:{q['version']}")]])

    def _capture_media(self,c,q):
        cards=[]
        for index in range(len(q['items'])):
            q=c.execute('SELECT * FROM media_queues WHERE id=%s',(q['id'],)).fetchone()
            card=self._capture_media_item(c,q,index)
            cards.append(card)
        q=c.execute('SELECT * FROM media_queues WHERE id=%s',(q['id'],)).fetchone()
        for index,card in enumerate(cards):
            if card is None:cards[index]=self._media_card(c,q,index)
        if not any(item['state']=='pending' for item in c.execute('SELECT items FROM media_queues WHERE id=%s',(q['id'],)).fetchone()['items']):c.execute("UPDATE media_queues SET state='done' WHERE id=%s",(q['id'],))
        return self._capture_combine(cards)

    def _capture_combine(self,cards):
        if not cards:return Reply('Нет операций для записи.')
        first=cards[0]
        first.additional_replies.extend(asdict(card) for card in cards[1:])
        return first

    def _resolve_capture_batch(self,actor,identity):
        cards=[]
        for index in range(8):
            with self._actor_transaction(actor) as c:
                batch=c.execute('SELECT * FROM input_batches WHERE id=%s',(UUID(identity),)).fetchone()
                if not batch:return Reply('Список недоступен.')
                if batch['capture_reply']:return Reply(**batch['capture_reply'])
                if index>=len(batch['items']):break
                item=batch['items'][index]
                if item.get('draft_id'):
                    d=c.execute('SELECT * FROM operation_drafts WHERE id=%s',(UUID(item['draft_id']),)).fetchone()
                    reply=self._capture_card(c,d['result_operation_id']) if d['result_operation_id'] else (Reply('Запись пропущена.') if d['state']=='cancelled' else self._prompt(c,d))
                else:
                    if self._draft(c):return Reply('Сначала завершите уточнение текущей записи.',[[('Открыть текущую запись','ui:resume')]])
                    reply=self._text_create_draft(c,batch['author_user_id'],batch['source_sent_at'],{'id':batch['workspace_id'],'account_id':batch['account_id'],'timezone':batch['timezone_snapshot']},item)
                    item['draft_id']=reply.capture_draft_id
                    c.execute('UPDATE input_batches SET items=%s WHERE id=%s',(Jsonb(batch['items']),batch['id']))
            if reply.job_id:reply=self._resolve_job(actor,reply.job_id)
            cards.append(reply)
            with self._actor_transaction(actor) as c:
                if self._draft(c):
                    if index+1<len(batch['items']):reply.additional_replies.append(asdict(Reply('После уточнения продолжите оставшиеся записи.',[[('Продолжить список','tqueue')]])))
                    break
        reply=self._capture_combine(cards)
        with self._actor_transaction(actor) as c:
            pending=c.execute("SELECT 1 FROM operation_drafts WHERE state='pending' AND id IN (SELECT (item->>'draft_id')::uuid FROM input_batches b, jsonb_array_elements(b.items) item WHERE b.id=%s)",(UUID(identity),)).fetchone()
            if not pending:c.execute("UPDATE input_batches SET state='done',capture_reply=%s WHERE id=%s",(Jsonb(asdict(reply)),UUID(identity)))
        return reply
