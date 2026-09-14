"""Receipt intake and review. OCR/HTTP run outside database transactions."""
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from balans.domain import Reply, amount_from_text, date_from_text, money
from balans.receipt_ai import ReceiptExtraction, number
from balans.receipt_media import MAX_BATCH_BYTES, MAX_PAGES, MediaError, Prepared, prepare


class Receipts:
    def _receipt_disabled(self):
        return Reply('Обработка чеков и скриншотов выключена. /receipts on — включить; /manual — ручной ввод.')

    def _receipt_gate(self,c):
        quota=self._quota_preflight(c,'image')
        if quota:return quota
        if self._media_queue(c):return Reply('Сначала завершите список изображений: /media; /media cancel — отменить оставшееся.')
        settings=c.execute('SELECT receipts_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
        if not settings or not settings['receipts_enabled']:
            return self._receipt_disabled()
        if not self.receipt_ai.available:
            return Reply('Распознавание чеков временно недоступно. Можно записать расход вручную: /manual.')
        if self._voice_busy(c):
            return Reply('Голос ещё обрабатывается. /voice — состояние; /cancel — отмена.')
        draft=self._draft(c)
        if draft:
            return Reply('Сначала сохраните или отмените текущий расход. /add — показать его; /cancel — отменить.')
        active=c.execute("SELECT id FROM receipt_batches WHERE state='processing' AND author_user_id=actor_user_id()").fetchone()
        if active:
            return Reply('Предыдущий чек ещё обрабатывается. /receipts — состояние; /cancel — отмена.')
        return None

    def receipt_preflight(self,telegram_id,bot_id,update_id):
        with self._actor_transaction(telegram_id) as c:
            c.execute('SELECT bootstrap()')
            old=c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot_id,update_id)).fetchone()
            if old:
                return self._safe_cached(c,old)
            c.execute("UPDATE receipt_batches SET state='cancelled' WHERE state='collecting' AND expires_at<=now()")
            c.execute("UPDATE operation_drafts SET state='cancelled' WHERE state='pending' AND expires_at<=now()")
            if self._document_upload(c):return None
            return self._receipt_gate(c)

    def receive_receipt(self,telegram_id,bot_id,update_id,sent_at,file_id,data,telegram_kind='document'):
        with self._actor_transaction(telegram_id) as c:
            c.execute('SELECT bootstrap()')
            attaching=bool(self._document_upload(c))
        if attaching:return self._receive_document(telegram_id,bot_id,update_id,data)
        gate=self.receipt_preflight(telegram_id,bot_id,update_id)
        if gate:
            return gate
        try:
            prepared=prepare(data)
        except MediaError as exc:
            return self.receipt_error(telegram_id,bot_id,update_id,str(exc))
        storage_id=uuid4()
        wrote=False
        try:
            with self._actor_transaction(telegram_id) as c:
                user_id=c.execute('SELECT actor_user_id() AS id').fetchone()['id']
                old=c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot_id,update_id)).fetchone()
                if old:
                    return self._safe_cached(c,old)
                reply=self._receipt_gate(c)
                if reply:
                    return reply
                batch=c.execute("SELECT * FROM receipt_batches WHERE author_user_id=%s AND state='collecting'",(user_id,)).fetchone()
                if not batch:
                    row=self._account_context(c)
                    batch=c.execute('INSERT INTO receipt_batches(workspace_id,author_user_id,account_id,source_sent_at,timezone_snapshot,model) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',
                                    (row['id'],user_id,row['account_id'],sent_at,row['timezone'],self.receipt_ai.model)).fetchone()
                counts=c.execute('SELECT coalesce(sum(page_count),0) AS pages,coalesce(sum(size_bytes),0) AS bytes FROM receipt_files WHERE batch_id=%s',(batch['id'],)).fetchone()
                sha=hashlib.sha256(data).hexdigest()
                duplicate=c.execute('SELECT id FROM receipt_files WHERE batch_id=%s AND sha256=%s',(batch['id'],sha)).fetchone()
                if duplicate:
                    reply=self._receipt_collection(c,batch)
                    reply.text='Этот файл уже добавлен в текущий чек.\n'+reply.text
                elif counts['pages']+prepared.pages>MAX_PAGES or counts['bytes']+len(data)>MAX_BATCH_BYTES:
                    reply=Reply('На один чек допускается до 10 страниц и 30 МБ. Этот файл не добавлен. /receipts — текущий чек; /cancel — начать заново.')
                else:
                    self.receipt_storage.put(storage_id,data);wrote=True
                    c.execute('INSERT INTO receipt_files(id,workspace_id,author_user_id,batch_id,bot_id,update_id,telegram_file_id,mime_type,sha256,size_bytes,page_count,telegram_kind) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (storage_id,batch['workspace_id'],user_id,batch['id'],bot_id,update_id,file_id,prepared.mime,sha,len(data),prepared.pages,telegram_kind))
                    batch=c.execute('UPDATE receipt_batches SET version=version+1 WHERE id=%s RETURNING *',(batch['id'],)).fetchone()
                    reply=self._receipt_collection(c,batch)
                c.execute('INSERT INTO telegram_updates(bot_id,update_id,user_id,response) VALUES(%s,%s,%s,%s)',(bot_id,update_id,user_id,Jsonb(asdict(reply))))
            return reply
        except BaseException:
            if wrote:
                self.receipt_storage.remove(storage_id)
            raise

    def receipt_error(self,telegram_id,bot_id,update_id,text):
        with self._actor_transaction(telegram_id) as c:
            user_id=c.execute('SELECT bootstrap() AS id').fetchone()['id']
            kind=next((k for k in ('text','image','voice','analysis') if f'({k})' in text),None)
            reply=self._quota_card(c,kind) if kind and 'Квота AI исчерпана' in text else Reply(text)
            c.execute('INSERT INTO telegram_updates(bot_id,update_id,user_id,response) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(bot_id,update_id,user_id,Jsonb(asdict(reply))))
            return self._safe_cached(c,c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot_id,update_id)).fetchone())

    def _receipt_collection(self,c,batch):
        # Internal upload receipt; transport resolves it before sending a message.
        return Reply('',[[('Распознать один чек',f"rprocess:{batch['id']}:{batch['version']}")]],receipt_job_id=str(batch['id']))

    def _receipt_command(self,c,user_id,command,arg):
        if command=='/cancel':
            cancelled=c.execute("UPDATE receipt_batches SET state='cancelled',version=version+1 WHERE author_user_id=%s AND state IN ('collecting','processing') RETURNING id",(user_id,)).fetchall()
            if cancelled and not self._draft(c):
                return Reply('Набор чека отменён. Сохранённые расходы не изменились.')
            return None
        if command!='/receipts':
            return None
        if arg=='on':
            return self._receipt_callback(c,user_id,'receipts_on')
        if arg=='off':
            c.execute('UPDATE user_settings SET receipts_enabled=false WHERE user_id=%s',(user_id,))
            c.execute("UPDATE receipt_batches SET state='cancelled',version=version+1 WHERE author_user_id=%s AND state IN ('collecting','processing')",(user_id,))
            return Reply('Отправка чеков в AI выключена. Уже сохранённые расходы доступны в истории.')
        if arg:
            return Reply('Используйте /receipts, /receipts on или /receipts off.')
        enabled=c.execute('SELECT receipts_enabled FROM user_settings WHERE user_id=%s',(user_id,)).fetchone()['receipts_enabled']
        if not enabled:
            return self._receipt_disabled()
        batch=c.execute("SELECT * FROM receipt_batches WHERE author_user_id=%s AND state IN ('collecting','processing') ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
        if batch:
            return self._receipt_collection(c,batch) if batch['state']=='collecting' else Reply('Чек обрабатывается…',receipt_job_id=str(batch['id']))
        d=self._draft(c)
        if d and d['receipt_batch_id']:
            return self._receipt_prompt(c,d)
        return Reply('Отправьте фото чека или PDF/JPEG/PNG документом. Распознавание начнётся автоматически. /receipts off — выключить.')

    def _receipt_callback(self,c,user_id,callback):
        if callback=='receipts_on':
            c.execute("UPDATE user_settings SET receipts_enabled=true,receipts_consent_version='receipt-ai-v2',receipts_consented_at=now() WHERE user_id=%s",(user_id,))
            return Reply('Обработка чеков включена. Теперь отправьте фотографию или PDF чека. /receipts off — выключить.')
        if callback.startswith('rsplit:'):
            return self._receipt_split(c,callback)
        action,_,args=callback.partition(':')
        if action in ('rprocess','rretry'):
            try:
                raw_id,version=args.split(':');batch_id=UUID(raw_id);version=int(version)
            except ValueError:
                return Reply('Недействительная кнопка чека.')
            batch=c.execute('SELECT * FROM receipt_batches WHERE id=%s AND author_user_id=%s',(batch_id,user_id)).fetchone()
            if not batch:
                return Reply('Чек недоступен.')
            if action=='rprocess' and batch['reply']:
                return Reply(**batch['reply'])
            if batch['version']!=version or batch['state']=='cancelled':
                return Reply('Состав чека изменился или отменён. /receipts — текущий набор.')
            if action=='rretry':
                other=c.execute("SELECT id FROM receipt_batches WHERE author_user_id=%s AND state IN ('collecting','processing') AND id<>%s",(user_id,batch_id)).fetchone()
                if other or self._draft(c):
                    return Reply('Сначала завершите или отмените текущий расход/чек.')
                if batch['state']!='failed':
                    return Reply('Повтор уже запрошен или чек обработан.')
                c.execute("UPDATE receipt_batches SET state='collecting',reply=NULL,result=NULL,error_code=NULL,version=version+1 WHERE id=%s",(batch_id,))
                return self._receipt_collection(c,c.execute('SELECT * FROM receipt_batches WHERE id=%s',(batch_id,)).fetchone())
            return Reply('Распознаю чек…',receipt_job_id=str(batch_id))
        if action in ('rpaid','rhold','rduplicate','rfield'):
            parts=args.split(':')
            try:
                draft_id=UUID(parts[0]);version=int(parts[-1])
            except (ValueError,IndexError):
                return Reply('Недействительная кнопка чека.')
            d=self._draft(c)
            if not d or not d['receipt_batch_id'] or d['id']!=draft_id or d['version']!=version:
                return Reply('Карточка чека устарела. /add — показать текущую.')
            if action=='rfield':
                field=parts[1] if len(parts)==3 else ''
                if field not in ('amount','date','merchant','currency'):
                    return Reply('Недействительное поле.')
                c.execute('UPDATE operation_drafts SET receipt_edit_field=%s,version=version+1 WHERE id=%s',(field,draft_id))
            else:
                if action=='rhold':
                    c.execute('UPDATE operation_drafts SET payment_confirmed=false,version=version+1 WHERE id=%s',(draft_id,))
                elif action=='rpaid':
                    c.execute('UPDATE operation_drafts SET payment_confirmed=true,version=version+1 WHERE id=%s',(draft_id,))
                else:
                    c.execute('UPDATE operation_drafts SET duplicate_confirmed=true,version=version+1 WHERE id=%s',(draft_id,))
            return self._receipt_prompt(c,self._draft(c))
        if action in ('rview','ritems'):
            parts=args.split(':')
            try:
                operation_id=UUID(parts[0]);page=int(parts[1]) if len(parts)==2 else 1
                if not 1<=page<=100:
                    raise ValueError
            except ValueError:
                return Reply('Недействительный чек.')
            row=c.execute('SELECT d.receipt_batch_id FROM operations o JOIN operation_drafts d ON d.id=o.source_draft_id WHERE o.id=%s',(operation_id,)).fetchone()
            if not row or not row['receipt_batch_id']:
                return Reply('Чек недоступен или не прикреплён к этому расходу.')
            if action=='ritems':
                items=c.execute('SELECT * FROM receipt_items WHERE batch_id=%s ORDER BY line_no LIMIT 11 OFFSET %s',(row['receipt_batch_id'],(page-1)*10)).fetchall()
                reply=Reply('Позиции чека:\n'+'\n'.join(self._line_text(item) for item in items[:10]) if items else 'На этой странице позиций нет.')
                if len(items)>10:
                    reply.buttons.append([('Далее',f'ritems:{operation_id}:{page+1}')])
                return reply
            return Reply('Исходный чек.',receipt_operation_id=str(operation_id))
        return None

    def _resolve_receipt_attachment(self,telegram_id,operation_id):
        # Re-authorize even if the incoming update is a replay of a cached response.
        with self._actor_transaction(telegram_id) as c:
            shared=c.execute("SELECT o.id FROM operations o JOIN workspaces w ON w.id=o.workspace_id JOIN operation_drafts d ON d.id=o.source_draft_id LEFT JOIN receipt_batches b ON b.id=d.receipt_batch_id WHERE o.id=%s AND (w.kind='shared' OR b.parent_batch_id IS NOT NULL)",(UUID(operation_id),)).fetchone()
            if shared:
                self._attach_source_documents(c,shared['id'])
                return self._document_card(c,self._document_set(c,'operation',shared['id']))
            files=c.execute('SELECT f.telegram_file_id,f.telegram_kind FROM operations o JOIN operation_drafts d ON d.id=o.source_draft_id JOIN receipt_files f ON f.batch_id=d.receipt_batch_id WHERE o.id=%s AND f.expires_at>now() ORDER BY f.created_at,f.id',(UUID(operation_id),)).fetchall()
            return Reply('Исходный чек.' if files else 'Исходник недоступен или срок хранения истёк; операция и позиции сохранены.',
                         document_ids=[f['telegram_file_id'] for f in files if f['telegram_kind']=='document'],
                         photo_ids=[f['telegram_file_id'] for f in files if f['telegram_kind']=='photo'])

    def _resolve_receipt(self,telegram_id,raw_id):
        batch_id=UUID(raw_id)
        with self._actor_transaction(telegram_id) as c:
            batch=c.execute('SELECT *,lease_until>now() AS leased,expires_at>now() AS fresh FROM receipt_batches WHERE id=%s FOR UPDATE',(batch_id,)).fetchone()
            if not batch:
                return Reply('Чек недоступен.')
            if batch['reply']:
                return Reply(**batch['reply'])
            allowed=c.execute('SELECT receipts_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
            if not allowed or not allowed['receipts_enabled'] or not batch['fresh'] or batch['state']=='cancelled':
                return self._finish_receipt(c,batch,ReceiptExtraction(error_code='cancelled'),False)
            if self._draft(c):
                return self._finish_receipt(c,batch,ReceiptExtraction(error_code='draft_exists'),False)
            if batch['state']=='processing':
                if batch['leased']:
                    return Reply('Чек ещё распознаётся. /receipts — проверить; /cancel — отменить.')
                return self._finish_receipt(c,batch,ReceiptExtraction(error_code='interrupted'))
            files=c.execute('SELECT * FROM receipt_files WHERE batch_id=%s ORDER BY created_at,id',(batch_id,)).fetchall()
            if not files:
                return Reply('Сначала отправьте файл чека.')
            categories=[{'id':str(cat['id']),'name':cat['name']} for cat in self._categories(c,batch['workspace_id'])]
            c.execute("UPDATE receipt_batches SET state='processing',prompt_version='receipt-v2',categories_snapshot=%s,lease_until=now()+interval '5 minutes' WHERE id=%s",(Jsonb(categories),batch_id))
            expected_version=batch['version']
        try:
            prepared_files=[prepare(self.receipt_storage.read(f['id'])) for f in files]
            prepared=Prepared('mixed',sum(p.pages for p in prepared_files),'\n'.join(p.text for p in prepared_files),[im for p in prepared_files for im in p.images])
            if prepared.pages>MAX_PAGES or len(prepared.text)>50000:
                raise MediaError('Combined limit')
            extraction=self.receipt_ai.extract(prepared,categories)
        except (MediaError,OSError):
            extraction=ReceiptExtraction(error_code='file_unavailable_or_invalid')
        with self._actor_transaction(telegram_id) as c:
            batch=c.execute('SELECT *,expires_at>now() AS fresh FROM receipt_batches WHERE id=%s FOR UPDATE',(batch_id,)).fetchone()
            if not batch:
                return Reply('Чек недоступен.')
            if batch['reply']:
                return Reply(**batch['reply'])
            allowed=c.execute('SELECT receipts_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
            valid=allowed and allowed['receipts_enabled'] and batch['fresh'] and batch['state']=='processing' and batch['version']==expected_version and not self._draft(c)
            return self._finish_receipt(c,batch,extraction,bool(valid))

    def _finish_receipt(self,c,batch,extraction,apply=True):
        result=extraction.result
        if not apply:
            state='cancelled';reply=Reply('Обработка отменена: чек, настройки или текущий расход изменились.')
        elif not result or extraction.error_code:
            state='failed';reply=Reply('Не удалось распознать чек. Можно повторить запрос, переснять файл или ввести расход через /manual.',[[('Повторить распознавание',f"rretry:{batch['id']}:{batch['version']}")]])
        elif result.get('transactions'):
            state='ready';reply=self._media_finish(c,batch,result)
        elif result['document_kind']!='expense':
            state='failed';reply=Reply('В файле несколько чеков, возврат или другой документ. Отправьте один чек обычной покупки. Этот документ не сохранён как расход.')
        elif result['currency'] not in (None,self._account_currency(c,batch['account_id'])):
            state='failed';reply=Reply(f"В чеке валюта {result['currency']}. Сейчас счёт поддерживает {self._account_currency(c,batch['account_id'])}; расход не сохранён. Автоматическая конвертация не выполняется.")
        else:
            today=batch['source_sent_at'].astimezone(ZoneInfo(batch['timezone_snapshot'])).date()
            day=None
            if result['occurred_on']:
                day=datetime.strptime(result['occurred_on'],'%Y-%m-%d').date()
                if day>today:
                    day=None
            if not day:
                day=today
            description=(result['merchant'] or '')
            if result['items']:
                description+=(' · ' if description else '')+', '.join(line['name'] for line in result['items'][:2])
            category=result['category_id']
            allowed={str(cat['id']) for cat in self._categories(c,batch['workspace_id'])}
            if category not in allowed:
                category=None
            draft=c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,amount,description,occurred_on,category_id,timezone_snapshot,source_sent_at,step,flow,category_source,receipt_batch_id,merchant,receipt_currency) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirm','manual','ai',%s,%s,%s) RETURNING *",
                            (batch['workspace_id'],batch['author_user_id'],batch['account_id'],number(result['total']),description[:500],day,UUID(category) if category else None,batch['timezone_snapshot'],batch['source_sent_at'],batch['id'],result['merchant'],result['currency'] or self._account_currency(c,batch['account_id']))).fetchone()
            c.execute('UPDATE operation_drafts SET external_reference_hash=%s,source_type=%s WHERE id=%s',(result.get('reference_hash'),'receipt',draft['id']))
            rule=self._rule(c,draft)
            if rule:
                c.execute("UPDATE operation_drafts SET category_id=%s,category_source='rule' WHERE id=%s",(UUID(rule),draft['id']))
            for index,line in enumerate(result['items'],start=1):
                c.execute('INSERT INTO receipt_items(workspace_id,author_user_id,batch_id,line_no,name,quantity,unit_price,line_total,discount) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                          (batch['workspace_id'],batch['author_user_id'],batch['id'],index,line['name'],number(line['quantity'],True),number(line['unit_price']),number(line['line_total']),number(line['discount'])))
            c.execute('UPDATE receipt_batches SET result=%s WHERE id=%s',(Jsonb(result),batch['id']))
            c.execute('UPDATE operation_drafts SET automatic_capture=%s WHERE id=%s',(self._capture_enabled(c),draft['id']))
            state='ready';reply=self._receipt_prompt(c,self._draft(c))
        c.execute('UPDATE receipt_batches SET state=%s,result=%s,reply=%s,error_code=%s,response_id=%s,input_tokens=%s,output_tokens=%s WHERE id=%s',
                  (state,Jsonb(result) if result else None,Jsonb(asdict(reply)),extraction.error_code,extraction.response_id,extraction.input_tokens,extraction.output_tokens,batch['id']))
        return reply

    def _receipt_duplicates(self,c,d):
        if d['amount'] is None or d['occurred_on'] is None:
            return []
        return c.execute("SELECT r.amount,r.occurred_on,r.description FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.source_draft_id<>%s AND o.state='active' AND o.kind='expense' AND ((r.amount=%s AND r.occurred_on=%s) OR (%s::text IS NOT NULL AND r.external_reference_hash=%s)) ORDER BY o.created_at DESC LIMIT 3",(d['id'],d['amount'],d['occurred_on'],d.get('external_reference_hash'),d.get('external_reference_hash'))).fetchall()

    def _line_text(self,line):
        amount=money(Decimal(line['line_total'])) if line['line_total'] is not None else 'сумма не прочитана'
        details=''
        if line['quantity'] is not None and line['unit_price'] is not None:
            details=f" ({Decimal(line['quantity']).normalize()} × {money(Decimal(line['unit_price']))})"
        if line['discount'] is not None and Decimal(line['discount'])>0:
            details+=f"; скидка {money(Decimal(line['discount']))}"
        return f"{line['line_no']}. {line['name']}{details} — {amount}"

    def _receipt_prompt(self,c,d):
        captured=self._capture_draft(c,d)
        if captured is not None:return captured
        from balans.domain import CURRENCY
        CURRENCY.set(self._account_currency(c,d['account_id']))
        field=d['receipt_edit_field']
        if field=='amount' or d['amount'] is None:
            return Reply('Итог покупки в валюте учёта: введите сумму вручную, например 850,50. /cancel — отмена.')
        if field=='currency' or d['receipt_currency'] is None:
            currency=self._account_currency(c,d['account_id'])
            c.execute('UPDATE operation_drafts SET receipt_currency=%s,receipt_edit_field=NULL,version=version+1 WHERE id=%s',(currency,d['id']))
            return self._receipt_prompt(c,self._draft(c))
        if field=='date' or d['occurred_on'] is None:
            return Reply('Проверьте дату покупки: отправьте ДД.ММ.ГГГГ, «сегодня» или «вчера». '
                         f"Дата отправки чека: {d['source_sent_at'].astimezone(ZoneInfo(d['timezone_snapshot'])):%d.%m.%Y}.")
        if field=='merchant':
            return Reply('Введите продавца / магазин (до 200 символов).')
        if d['category_id'] is None or d['step']=='category':
            c.execute("UPDATE operation_drafts SET step='category' WHERE id=%s",(d['id'],))
            return self._category_menu(c,d)
        category=c.execute('SELECT name FROM categories WHERE id=%s',(d['category_id'],)).fetchone()['name']
        batch=c.execute('SELECT result FROM receipt_batches WHERE id=%s',(d['receipt_batch_id'],)).fetchone()
        result=batch['result'] or {}
        rows=c.execute('SELECT * FROM receipt_items WHERE batch_id=%s ORDER BY line_no LIMIT 6',(d['receipt_batch_id'],)).fetchall()
        preview='\n'.join(self._line_text(line) for line in rows[:5])
        if len(rows)>5:
            preview+='\nОстальные позиции доступны после сохранения в истории.'
        warnings=list(result.get('warnings',[]))
        if result.get('payment_status') in ('unknown','pending','failed'):
            warnings.append('На изображении нет надёжного подтверждения успешной оплаты.')
        duplicates=self._receipt_duplicates(c,d)
        if duplicates and not d['duplicate_confirmed']:
            warnings.append('Найден сохранённый расход с той же суммой и датой. Проверьте, не повторная ли это запись.')
        text=(f"Продавец: {d['merchant'] or 'не прочитан'}\n"
              f"Итог: {money(d['amount'])}\nДата: {d['occurred_on']:%d.%m.%Y}\nКатегория: {category}\n\n{preview}\n\n"+'\n'.join(warnings))
        buttons=[]
        if not d['payment_confirmed']:
            text+='\nПокупка оплачена?'
            buttons.append([('Да, покупка оплачена',f"rpaid:{d['id']}:{d['version']}"),('Ещё не оплачена',f"rhold:{d['id']}:{d['version']}")])
        elif duplicates and not d['duplicate_confirmed']:
            buttons.append([('Это другая покупка',f"rduplicate:{d['id']}:{d['version']}")])
        else:
            buttons.append([('Сохранить расход',f"save:{d['id']}:{d['version']}")])
        buttons.extend([[(label,f"rfield:{d['id']}:{field}:{d['version']}") for label,field in [('Сумма','amount'),('Дата','date'),('Магазин','merchant')]],
                        [('Выбрать другую категорию',f"recat:{d['id']}:{d['version']}"),('Отмена',f"cancel:{d['id']}:{d['version']}")]])
        if len(result.get('items',[]))>1:
            buttons.insert(0,[('Разбить по категориям',f"rsplit:{d['id']}:{d['version']}")])
        return Reply(text,buttons)

    def _receipt_split(self,c,callback):
        _,identity,version=callback.split(':')
        d=self._draft(c)
        if not d or str(d['id'])!=identity or d['version']!=int(version) or not d['receipt_batch_id']:
            return Reply('Карточка устарела. /add — текущий расход.')
        batch=c.execute('SELECT * FROM receipt_batches WHERE id=%s',(d['receipt_batch_id'],)).fetchone()
        result=batch['result']
        parent=dict(kind='expense',amount=str(d['amount']),currency=d['receipt_currency'],occurred_on=str(d['occurred_on']) if d['occurred_on'] else None,
                    merchant=d['merchant'],description=d['description'],category_id=str(d['category_id']) if d['category_id'] else None,
                    payment_status=result['payment_status'],account_hint=None,reference_hash=result.get('reference_hash'),split_group=str(batch['id']),
                    items=result['items'],items_complete=result['items_complete'],warnings=result.get('warnings',[]))
        items=self._split_lines(c,parent,batch['workspace_id'])
        if isinstance(items,Reply):return items
        c.execute("UPDATE operation_drafts SET state='cancelled',version=version+1 WHERE id=%s",(d['id'],))
        return self._media_finish(c,batch,dict(transactions=items,source_type='receipt'))

    def _receipt_text(self,c,d,text):
        if not d or not d['receipt_batch_id']:
            return None
        field=d['receipt_edit_field']
        if field=='amount' or d['amount'] is None:
            amount=amount_from_text(text)
            c.execute('UPDATE operation_drafts SET amount=%s,receipt_edit_field=NULL,duplicate_confirmed=false,version=version+1 WHERE id=%s',(amount,d['id']))
        elif field=='currency' or d['receipt_currency'] is None:
            currency=self._account_currency(c,d['account_id'])
            value='RUB' if text.upper() in ('РУБ','РУБЛИ','₽') else text.upper()
            if value!=currency:return Reply('Валюта чека не совпадает со счётом '+currency+'. /cancel — отменить и выбрать другой счёт.')
            c.execute("UPDATE operation_drafts SET receipt_currency=%s,receipt_edit_field=NULL,version=version+1 WHERE id=%s",(currency,d['id']))
        elif field=='date' or d['occurred_on'] is None:
            day=date_from_text(text,d['source_sent_at'],d['timezone_snapshot'])
            c.execute('UPDATE operation_drafts SET occurred_on=%s,receipt_edit_field=NULL,duplicate_confirmed=false,version=version+1 WHERE id=%s',(day,d['id']))
        elif field=='merchant':
            if not 1<=len(text)<=200:
                return Reply('Название магазина: от 1 до 200 символов.')
            from balans.receipt_ai import mask
            c.execute('UPDATE operation_drafts SET merchant=%s,description=%s,receipt_edit_field=NULL,version=version+1 WHERE id=%s',(mask(text),mask(text),d['id']))
        elif d['step']=='category' or d['category_id'] is None:
            category=c.execute('SELECT id FROM categories WHERE workspace_id=%s AND lower(name)=lower(%s)',(d['workspace_id'],text)).fetchone()
            if not category:
                return self._category_menu(c,d)
            return self._select_category(c,d,category['id'])
        return self._receipt_prompt(c,self._draft(c))
