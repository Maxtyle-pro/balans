"""Individually confirmed operations extracted from financial images."""
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from balans.domain import Reply,amount_from_text,date_from_text,money
from balans.finance import KINDS
from balans.receipt_ai import mask,number

class MediaFlow:
    def _media_queue(self,c):
        c.execute("UPDATE media_queues SET state='cancelled' WHERE state='active' AND expires_at<=now()")
        return c.execute("SELECT * FROM media_queues WHERE state='active'").fetchone()

    def _media_finish(self,c,batch,result):
        today=batch['source_sent_at'].astimezone(ZoneInfo(batch['timezone_snapshot'])).date()
        items=[]
        for raw in result['transactions']:
            item=dict(raw)
            if item['occurred_on'] and datetime.strptime(item['occurred_on'],'%Y-%m-%d').date()>today:item['occurred_on']=None
            item.update(state='pending',account=str(batch['account_id']),destination=None,refund=None,paid=False,duplicate_confirmed=False,source_type=result.get('source_type') if result.get('source_type') in ('receipt','screenshot','terminal') else None)
            if item['kind']=='expense':
                rule=self._rule(c,{'workspace_id':batch['workspace_id'],'description':item['description'] or item['merchant']})
                if rule:item['category_id']=rule
            item['suggested_category']=item['category_id']
            items.append(item)
        queue=c.execute('INSERT INTO media_queues(workspace_id,author_user_id,source_batch_id,items) VALUES(%s,%s,%s,%s) RETURNING *',(batch['workspace_id'],batch['author_user_id'],batch['id'],Jsonb(items))).fetchone()
        return self._media_card(c,queue,0) if len(items)==1 else self._media_list(c,queue,grouped=True)

    def _media_blocker(self,c):
        q=self._media_queue(c)
        if not q:return Reply('Список уже завершён.')
        return Reply('Сначала завершите список изображений или отмените оставшиеся позиции. Сохранённые операции останутся.',[[('Продолжить список',f"mlist:{q['id']}"),('Отменить список',f"mstop:{q['id']}")]])

    def _media_list(self,c,q,grouped=False):
        if not q:return Reply('Отправьте чек или скриншот.')
        pending=[i for i,item in enumerate(q['items']) if item['state']=='pending']
        if not pending:c.execute("UPDATE media_queues SET state='done' WHERE id=%s",(q['id'],))
        if not pending:
            saved=sum(item['state']=='saved' for item in q['items'])
            text=(f'✅ Готово! Сохранено операций: {saved}.'+('\nОстальные позиции пропущены.' if any(item['state']=='excluded' for item in q['items']) else '') if saved else 'Список завершён. Вы пропустили все позиции — ничего не сохранено.')
            return Reply(text,[[('🕘 Открыть историю','history'),('➕ Добавить расход','add')],[('☰ Все действия','ui:menu')]])
        categories={str(cat['id']):cat['name'] for cat in self._categories(c,q['workspace_id'])}
        totals={};groups={};rows=[]
        visible=set(pending[:8] if pending else range(min(8,len(q['items']))))
        for i,item in enumerate(q['items']):
            if item['state']=='excluded':continue
            category=categories.get(item['category_id'],'Выберите категорию') if item['kind']=='expense' else KINDS.get(item['kind'],'Уточните тип')
            amount=money(Decimal(item['amount']),item['currency']) if item['amount'] else 'Уточните сумму'
            if i in visible:rows.append(f"{i+1}. {item['description'] or item['merchant'] or 'Операция'} — {amount}\n{category} · {item['occurred_on'] or 'Уточните дату'}"+(' · Сохранено' if item['state']=='saved' else ''))
            if item['amount']:
                key=(item['kind'],item['currency'] or '?');totals[key]=totals.get(key,Decimal(0))+Decimal(item['amount'])
                key=(category,item['kind'],item['currency'] or '?');groups[key]=groups.get(key,Decimal(0))+Decimal(item['amount'])
        text='📷 Операции на изображении\n\n'+'\n\n'.join(rows)
        if totals:text+='\n\nСумма в списке: '+'; '.join(f"{KINDS.get(kind,'Операции')}: {money(value,currency)}" for (kind,currency),value in totals.items())
        if grouped and groups:text+='\nПо категориям:\n'+'\n'.join(f"{category}: {money(value,currency)}" for (category,kind,currency),value in groups.items())
        text+='\n\nОткройте операцию, уточните данные и подтвердите сохранение. «Пропустить» уберёт её из списка без записи в расходы.'
        buttons=[]
        if len(pending)>1:
            buttons.append([('Разбить по категориям',f"mgroup:{q['id']}:{q['version']}")])
            if any(q['items'][i]['kind']=='expense' for i in pending):buttons.append([('Выбрать другую категорию',f"mallcat:{q['id']}:{q['version']}")])
        if len(pending)>8:text+=f'\nЕщё позиций: {len(pending)-8}. Следующие появятся после обработки показанных.'
        buttons += [[((f'✏️ Открыть операцию {i+1}' if len(pending)>1 else '✏️ Открыть операцию'),f"mreview:{q['id']}:{i}:{q['version']}"),((f'Пропустить {i+1}' if len(pending)>1 else 'Пропустить'),f"mdrop:{q['id']}:{i}:{q['version']}")] for i in pending[:8]]
        if pending:buttons.append([('Отменить оставшееся',f"mstop:{q['id']}")])
        return Reply(text,buttons)

    def _split_lines(self,c,parent,workspace):
        lines=parent.get('items',[])
        amounts=[number(line.get('line_total')) for line in lines]
        if len(lines)<2 or not parent.get('items_complete') or not parent.get('amount') or any(a is None or a<=0 for a in amounts):
            return Reply('Для разбиения нужен полный список позиций с суммами. Пришлите полный чек или исправьте данные. /media — результат.')
        if sum(amounts)!=Decimal(parent['amount']):
            return Reply('Сумма позиций не совпадает с итогом. Проверьте итог, скидки и полноту чека перед разбиением.')
        categories={str(cat['id']) for cat in self._categories(c,workspace)}
        result=[]
        for line,amount in zip(lines,amounts):
            category=self._rule(c,{'workspace_id':workspace,'description':line['name']}) or line.get('category_id') or parent.get('category_id')
            item=dict(parent,amount=str(amount),description=line['name'],category_id=category if category in categories else None,
                      items=[line],items_complete=True,warnings=[])
            # Keep the source reference to detect a later re-upload of the whole receipt.
            result.append(item)
        return result

    def _media_categories(self,c,q,index):
        categories=self._categories(c,q['workspace_id'])
        q['items'][max(index,0)]['category_choices']=[str(cat['id']) for cat in categories]
        q=c.execute('UPDATE media_queues SET items=%s,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
        return Reply('Выберите категорию:',[[(cat['name'],f"mc:{q['id']}:{q['version']}:{index}:{i}")] for i,cat in enumerate(categories)]+[[('Назад',f"mlist:{q['id']}")]])

    def _media_category_callback(self,c,q,action,raw):
        parts=raw.split(':')
        if len(parts)<2 or parts[0]!=str(q['id']) or int(parts[1])!=q['version']:
            return Reply('Карточка устарела. /media — текущий результат.')
        if action=='mallcat':return self._media_categories(c,q,-1)
        if action=='mgroup':
            # Independent transactions remain independent; only their category totals are grouped.
            return self._media_list(c,q,grouped=True)
        index=int(parts[2])
        if index!=-1 and not 0<=index<len(q['items']):return Reply('Позиция недоступна.')
        if action=='msplit':
            item=q['items'][index]
            if item['state']!='pending' or item['kind']!='expense':return self._media_list(c,q)
            item=dict(item,split_group=str(q['source_batch_id']))
            items=self._split_lines(c,item,q['workspace_id'])
            if isinstance(items,Reply):return items
            for child in items:
                child.update(state='pending',paid=False,duplicate_confirmed=False,suggested_category=child['category_id'])
            q['items'][index:index+1]=items
        elif action=='mc':
            choices=q['items'][max(index,0)].get('category_choices',[]);choice=int(parts[3])
            allowed={str(cat['id']) for cat in self._categories(c,q['workspace_id'])}
            if not 0<=choice<len(choices) or choices[choice] not in allowed:return Reply('Категория недоступна.')
            for i,item in enumerate(q['items']):
                if (index==-1 or index==i) and item['state']=='pending' and item['kind']=='expense':
                    item['category_id']=choices[choice]
        q=c.execute('UPDATE media_queues SET items=%s,selected_index=NULL,edit_field=NULL,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
        return self._media_list(c,q,grouped=action=='msplit') if index==-1 or action=='msplit' else self._media_card(c,q,index)

    def _media_duplicates(self,c,item):
        if not item.get('amount') or not item.get('occurred_on'):return []
        return c.execute("SELECT o.id,r.description,r.amount,r.occurred_on FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.state='active' AND o.kind=%s AND ((r.amount=%s AND r.occurred_on=%s) OR (%s::text IS NOT NULL AND r.external_reference_hash=%s)) AND NOT EXISTS(SELECT 1 FROM operation_drafts d JOIN receipt_batches b ON b.id=d.receipt_batch_id WHERE d.id=o.source_draft_id AND b.parent_batch_id=%s::uuid) ORDER BY o.created_at DESC LIMIT 3",(item['kind'],Decimal(item['amount']),item['occurred_on'],item.get('reference_hash'),item.get('reference_hash'),item.get('split_group'))).fetchall()

    def _media_card(self,c,q,index):
        item=q['items'][index]
        if item['state']!='pending':return self._media_list(c,q)
        c.execute('UPDATE media_queues SET selected_index=%s,edit_field=NULL WHERE id=%s',(index,q['id']))
        category=c.execute('SELECT name FROM categories WHERE id=%s',(UUID(item['category_id']),)).fetchone() if item['category_id'] else None
        text=f"Операция {index+1}\nТип: {KINDS.get(item['kind'],'Выберите тип')}\nСумма: {item['amount'] or '?'} {item['currency'] or '?'}\nДата: {item['occurred_on'] or 'Не прочитана — укажите вручную'}\nКонтрагент: {item['merchant'] or 'Не прочитан'}\nОписание: {item['description']}\nКатегория: {category['name'] if category else 'Не выбрана'}"
        if item['kind']=='income' and c.execute("SELECT kind='shared' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']:
            text+='\nЭто будет заявленный приход, ожидающий сверки руководителем. Если деньги относятся к выдаче, подтвердите её через /funds вместо нового прихода.'
        if item['items']:
            text+='\nПозиции:\n'+'\n'.join(self._line_text(dict(line,line_no=i)) for i,line in enumerate(item['items'][:5],1))
            if len(item['items'])>5:text+='\nОстальные позиции доступны в сохранённом оригинале.'
        if item.get('warnings'):text+='\n'+'\n'.join(item['warnings'])
        if item['destination']:text+='\nПолучатель: '+self._account_name(c,UUID(item['destination']))
        if item['refund']:text+='\nИсходная покупка: '+item['refund']
        suffix=f"{q['id']}:{index}:{q['version']}";buttons=[]
        if not item['source_type']:text+='\nВыберите тип изображения'
        if not item['source_type']:buttons.append([(label,f'msource:{value}:{suffix}') for value,label in [('receipt','Чек'),('screenshot','Скриншот'),('terminal','Касса/терминал')]])
        buttons.append([(label,f'mkind:{kind}:{suffix}') for kind,label in [('expense','Расход'),('income','Доход'),('refund','Возврат'),('transfer','Между своими счетами')]])
        buttons.append([(label,f'mf:{field}:{suffix}') for field,label in [('amount','Сумма'),('date','Дата'),('currency','Валюта')]])
        buttons.append([(label,f'mf:{field}:{suffix}') for field,label in [('merchant','Контрагент'),('description','Описание'),('account','Счёт')]])
        if item['kind']=='expense':
            buttons.append([('Выбрать другую категорию',f'mf:category:{suffix}')])
            if len(item['items'])>1:buttons.insert(0,[('Разбить по категориям',f"msplit:{q['id']}:{q['version']}:{index}")])
        if item['kind']=='transfer':buttons.append([('Счёт получателя',f'mf:destination:{suffix}')])
        if item['kind']=='refund':buttons.append([('Исходная покупка',f'mf:refund:{suffix}')])
        duplicates=self._media_duplicates(c,item)
        if duplicates and not item['duplicate_confirmed']:
            text+='\nВозможный дубль: совпала сумма/дата или идентификатор. Можно прикрепить изображение к существующей записи.'
            buttons.append([('Это отдельная операция',f'mduplicate:{suffix}')])
            for r in duplicates:buttons.append([(f"Прикрепить к {r['description'][:35]}",f"mlink:{r['id']}")])
        if not item['paid']:
            text+='\nПодтвердите факт совершения операции. Статус ожидания/отказа на изображении сам по себе не подтверждает оплату.'
            buttons.append([('Операция совершена',f'mpaid:{suffix}'),('Ещё не совершена',f'mhold:{suffix}')])
        elif not duplicates or item['duplicate_confirmed']:
            buttons.append([('Сохранить эту строку',f"msave:{suffix}")])
        buttons.append([('К списку',f"mlist:{q['id']}")])
        return Reply(text,buttons)

    def _media_text(self,c,q,text):
        index=q['selected_index'];field=q['edit_field']
        if index is None or not field:return self._media_list(c,q)
        item=q['items'][index]
        if item['state']!='pending':return self._media_list(c,q)
        batch=c.execute('SELECT * FROM receipt_batches WHERE id=%s',(q['source_batch_id'],)).fetchone()
        if field=='amount':item['amount']=str(amount_from_text(text))
        elif field=='date':item['occurred_on']=str(date_from_text(text,batch['source_sent_at'],batch['timezone_snapshot']))
        elif field=='currency':
            if text.upper()!=self._account_currency(c,UUID(item['account'])):raise ValueError('Валюта должна совпадать с выбранным счётом. Смените счёт или исключите строку.')
            item['currency']=text.upper()
        elif field in ('account','destination'):
            a=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.user_id=actor_user_id() AND lower(a.name)=lower(%s)',(text,)).fetchone()
            if not a:raise ValueError('Свой счёт не найден. /accounts — список.')
            item[field]=str(a['id'])
        elif field=='category':
            cat=c.execute('SELECT id FROM categories WHERE NOT archived AND lower(name)=lower(%s)',(text,)).fetchone()
            if not cat:raise ValueError('Категория не найдена. /categories — список.')
            item['category_id']=str(cat['id'])
        elif field=='refund':
            r=c.execute("SELECT o.id,r.category_id FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.created_by_user_id=actor_user_id() AND o.state='active' AND o.kind='expense' AND o.id=%s",(UUID(text),)).fetchone()
            if not r:raise ValueError('Исходная покупка недоступна.')
            item['refund']=str(r['id']);item['category_id']=str(r['category_id'])
        else:
            if not 1<=len(text)<=(200 if field=='merchant' else 500):raise ValueError('Слишком короткое или длинное значение.')
            item[field]=mask(text)
        item['duplicate_confirmed']=False;item['paid']=False
        q=c.execute('UPDATE media_queues SET items=%s,edit_field=NULL,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
        return self._media_card(c,q,index)

    def _media_save(self,c,q,index):
        item=q['items'][index]
        if not item['source_type'] or not item['paid'] or item['kind']=='unknown' or not item['amount'] or not item['occurred_on'] or item['currency']!=self._account_currency(c,UUID(item['account'])):raise ValueError('Подтвердите источник, тип, сумму, дату, валюту счёта и факт совершения операции.')
        if item['kind']=='expense' and not item['category_id']:raise ValueError('Выберите категорию.')
        if item['kind']=='transfer' and not item['destination']:raise ValueError('Выберите счёт получателя.')
        if item['kind']=='refund' and not item['refund']:raise ValueError('Выберите исходную покупку.')
        if self._media_duplicates(c,item) and not item['duplicate_confirmed']:return self._media_card(c,q,index)
        if self._draft(c):raise ValueError('Сначала завершите открытый черновик или /cancel.')
        batch=c.execute('SELECT * FROM receipt_batches WHERE id=%s',(q['source_batch_id'],)).fetchone()
        shared=c.execute("SELECT kind='shared' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']
        feedback=None
        with c.transaction():
            if item['kind']=='income' and shared:
                if not item['merchant'] or not item['description']:raise ValueError('Для заявленного прихода нужны источник и назначение.')
                identity=self._workspace_call(c,'SELECT create_fund_claim(%s,%s,%s,%s,%s) AS id',(UUID(item['account']),Decimal(item['amount']),item['occurred_on'],item['merchant'],item['description']))['id']
                ds=self._document_set(c,'claim',identity)
                for f in c.execute('SELECT * FROM receipt_files WHERE batch_id=%s',(batch['id'],)).fetchall():
                    try:self._store_document(c,ds,self.receipt_storage.read(f['id']),f['mime_type'],receipt=f['id'])
                    except (ValueError,OSError):pass
                kind='claim'
            else:
                child=c.execute("INSERT INTO receipt_batches(workspace_id,author_user_id,account_id,source_sent_at,timezone_snapshot,model,state,parent_batch_id,result) VALUES(%s,%s,%s,%s,%s,%s,'ready',%s,%s) RETURNING id",(batch['workspace_id'],batch['author_user_id'],UUID(item['account']),batch['source_sent_at'],batch['timezone_snapshot'],batch['model'],batch['id'],Jsonb({'payment_status':item['payment_status'],'warnings':[]}))).fetchone()['id']
                d=c.execute("INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,amount,description,occurred_on,category_id,timezone_snapshot,source_sent_at,step,kind,destination_account_id,refund_of,receipt_batch_id,receipt_currency,payment_confirmed,duplicate_confirmed,merchant,source_type,external_reference_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirm',%s,%s,%s,%s,%s,true,%s,%s,%s,%s) RETURNING id",(q['workspace_id'],q['author_user_id'],UUID(item['account']),Decimal(item['amount']),item['description'] or item['merchant'] or 'Операция по изображению',item['occurred_on'],UUID(item['category_id']) if item['category_id'] else None,batch['timezone_snapshot'],batch['source_sent_at'],item['kind'],UUID(item['destination']) if item['destination'] else None,UUID(item['refund']) if item['refund'] else None,child,item['currency'],bool(item['duplicate_confirmed'] or item.get('split_group')),item['merchant'],item['source_type'],item.get('reference_hash'))).fetchone()['id']
                for line_no,line in enumerate(item['items'],1):c.execute('INSERT INTO receipt_items(workspace_id,author_user_id,batch_id,line_no,name,quantity,unit_price,line_total,discount) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(q['workspace_id'],q['author_user_id'],child,line_no,line['name'],number(line['quantity'],True),number(line['unit_price']),number(line['line_total']),number(line['discount'])))
                identity=self._save_operation(c,d);kind='operation'
                if item['kind']=='expense' and item.get('suggested_category')!=item['category_id']:
                    operation=c.execute('SELECT o.current_revision_id,r.description FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.id=%s',(identity,)).fetchone()
                    feedback=c.execute('INSERT INTO category_feedback(workspace_id,author_user_id,operation_id,old_category_id,new_category_id,description,expected_revision_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(q['workspace_id'],q['author_user_id'],identity,UUID(item['suggested_category']) if item.get('suggested_category') else None,UUID(item['category_id']),operation['description'],operation['current_revision_id'])).fetchone()['id']
            item['state']='saved';item['result_id']=str(identity);item['result_kind']=kind
            q=c.execute('UPDATE media_queues SET items=%s,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
        reply=self._media_list(c,q);reply.text=('Приход заявлен и ожидает сверки. ' if kind=='claim' else 'Строка сохранена. ')+reply.text
        reply.buttons.append([('Документы записи',f'docview:{kind}:{identity}')]);return self._learning_offer(c,feedback,reply)

    def _media_callback(self,c,user,callback,sent):
        action,_,raw=callback.partition(':')
        if action not in ('mstop','mreview','mdrop','mlist','mkind','msource','mf','mpaid','mhold','mduplicate','msave','mlink','mlinkok','msplit','mallcat','mc','mgroup'):return None
        q=self._media_queue(c)
        if not q:return Reply('Список завершён или недоступен. Повторных записей нет.')
        if action in ('msplit','mallcat','mc','mgroup'):return self._media_category_callback(c,q,action,raw)
        if action=='mlink':
            if q['selected_index'] is None:return self._media_list(c,q)
            item=q['items'][q['selected_index']]
            candidates=self._media_duplicates(c,item)
            if UUID(raw) not in [r['id'] for r in candidates]:return Reply('Запись недоступна для сопоставления.')
            token=c.execute('INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s) RETURNING id',(Jsonb({'action':'media_link','queue':str(q['id']),'index':q['selected_index'],'version':q['version'],'operation':raw}),)).fetchone()['id']
            return Reply('Прикрепить изображения к существующей операции '+raw+'? Нового расхода не будет.',[[('Прикрепить без нового расхода',f"mlinkok:{token}")]])
        if action=='mlinkok':
            token=c.execute("SELECT * FROM fund_confirmations WHERE id=%s AND state='pending' AND expires_at>now()",(UUID(raw),)).fetchone()
            if not token or token['payload'].get('action')!='media_link':return Reply('Подтверждение недоступно.')
            p=token['payload']
            if p['queue']!=str(q['id']) or p['version']!=q['version']:return Reply('Список изменился. Повторите сопоставление.')
            ds=self._document_set(c,'operation',UUID(p['operation']))
            for f in c.execute('SELECT * FROM receipt_files WHERE batch_id=%s',(q['source_batch_id'],)).fetchall():self._store_document(c,ds,self.receipt_storage.read(f['id']),f['mime_type'],receipt=f['id'])
            q['items'][p['index']]['state']='saved';q['items'][p['index']]['result_id']=p['operation']
            c.execute("UPDATE fund_confirmations SET state='done' WHERE id=%s",(token['id'],))
            q=c.execute('UPDATE media_queues SET items=%s,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
            return self._media_list(c,q)
        value=None
        if action in ('mkind','msource','mf'):value,_,raw=raw.partition(':')
        parts=raw.split(':')
        if UUID(parts[0])!=q['id']:return Reply('Список недоступен.')
        if action=='mstop':
            c.execute("UPDATE media_queues SET state='cancelled' WHERE id=%s",(q['id'],))
            return Reply('Оставшиеся позиции отменены. Сохранённые операции остались.',[[('➕ Добавить расход','add'),('☰ Все действия','ui:menu')]])
        if action=='mlist':return self._media_list(c,q)
        if len(parts)!=3 or int(parts[2])!=q['version']:return Reply('Карточка устарела. /media — текущий результат.')
        index=int(parts[1])
        if not 0<=index<len(q['items']):return Reply('Строка недоступна.')
        item=q['items'][index]
        if item['state']!='pending':return self._media_list(c,q)
        if action=='mreview':return self._media_card(c,q,index)
        if action=='msave':
            if len(parts)!=3 or int(parts[2])!=q['version']:return Reply('Карточка устарела. /media — откройте текущую строку.')
            return self._media_save(c,q,index)
        if action=='mf':
            if value not in ('amount','date','currency','merchant','description','account','destination','refund','category'):return Reply('Поле недоступно.')
            c.execute('UPDATE media_queues SET selected_index=%s,edit_field=%s,version=version+1 WHERE id=%s',(index,value,q['id']))
            if value=='category':
                q=c.execute('SELECT * FROM media_queues WHERE id=%s',(q['id'],)).fetchone()
                return self._media_categories(c,q,index)
            hints={'amount':'сумму','date':'дату ДД.ММ.ГГГГ, сегодня или вчера','currency':'код валюты документа: RUB, USD или EUR','merchant':'контрагента','description':'описание','account':'точное название своего счёта','destination':'точное название своего счёта получателя','refund':'ID исходной покупки из её карточки документов','category':'название категории из /categories'}
            return Reply('Введите '+hints[value]+'. /media — вернуться к списку.')
        if action=='mkind':
            if value not in ('expense','income','refund','transfer'):return Reply('Тип недоступен.')
            item['kind']=value;item['paid']=False;item['duplicate_confirmed']=False
            if value!='refund':item['refund']=None
            if value!='transfer':item['destination']=None
            if value in ('income','transfer'):item['category_id']=None
        elif action=='msource':
            if value not in ('receipt','screenshot','terminal'):return Reply('Источник недоступен.')
            item['source_type']=value;item['paid']=False
        elif action=='mdrop':item['state']='excluded'
        elif action=='mpaid':item['paid']=True
        elif action=='mhold':item['paid']=False
        elif action=='mduplicate':item['duplicate_confirmed']=True
        q=c.execute('UPDATE media_queues SET items=%s,version=version+1 WHERE id=%s RETURNING *',(Jsonb(q['items']),q['id'])).fetchone()
        return self._media_list(c,q) if action=='mdrop' else self._media_card(c,q,index)

    def _media_command(self,c,user,command,arg,sent):
        if command not in ('/media','/cancel'):return None
        q=self._media_queue(c)
        if command=='/cancel' and not q:return None
        if q and (command=='/cancel' or arg=='cancel'):
            c.execute("UPDATE media_queues SET state='cancelled' WHERE id=%s",(q['id'],))
            return Reply('Оставшиеся строки отменены. Сохранённые операции остаются в истории.')
        return self._media_list(c,q)
