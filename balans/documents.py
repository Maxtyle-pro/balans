"""Private original attachments, review workflow and closed periods."""
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID,uuid4
import base64
import hashlib
from psycopg.types.json import Jsonb
from balans.domain import Reply,money,amount_from_text
from balans.receipt_media import ReceiptStorage,MediaError,prepare

DOC_STATUS={'unreviewed':'Не проверено','accepted':'Принято','clarification':'Требует уточнения'}

class DocumentStorage:
    def __init__(self,root):
        self.personal=ReceiptStorage(root/'personal');self.shared=ReceiptStorage(root/'shared')
    def put(self,identity,data,permanent):
        (self.shared if permanent else self.personal).put(identity,data)
    def read(self,identity,permanent):
        if not permanent:return self.personal.read(identity)
        path=self.shared.path(identity)
        if not path.is_file() or path.is_symlink():raise MediaError('Оригинал недоступен в хранилище.')
        if path.stat().st_size>15*1024*1024:raise MediaError('Оригинал превышает допустимый размер.')
        return path.read_bytes()
    def remove(self,identity,permanent):
        (self.shared if permanent else self.personal).remove(identity)
    def purge(self):self.personal.purge()

class Documents:
    def _document_upload(self,c):
        c.execute("UPDATE document_uploads SET state='cancelled' WHERE state='pending' AND expires_at<=now()")
        return c.execute("SELECT * FROM document_uploads WHERE state='pending'").fetchone()

    def _document_set(self,c,kind,identity):
        self._workspace_call(c,'SELECT ensure_document_set(%s,%s)',(kind,UUID(str(identity))))
        return c.execute('SELECT *,owns_workspace(workspace_id) AS manager FROM document_sets WHERE id=%s',(identity,)).fetchone()

    def _document_card(self,c,ds,page=1):
        if not 1<=page<=20:raise ValueError('Страница недоступна.')
        docs=c.execute("SELECT *,expires_at IS NULL OR expires_at>now() AS fresh FROM documents WHERE set_id=%s ORDER BY created_at,id LIMIT 100",(ds['id'],)).fetchall()
        active=[d for d in docs if d['state']=='active' and d['fresh']]
        text=f"Документы · {self._workspace_name(c)}\nЗапись: {ds['id']}\n{ds['occurred_on']} · {money(ds['amount'],ds['currency'])}\n{DOC_STATUS[ds['status']]} · "+(f'Документ приложен: {len(active)}' if active else 'Без документа')
        if ds['request_text']:text+='\nЗапрос: '+ds['request_text']
        if ds['exception_reason']:text+='\nИсключение: '+ds['exception_reason']
        buttons=[[('Добавить чек/документ',f"docadd:{ds['entity_kind']}:{ds['id']}")]]
        for i,d in enumerate(docs[(page-1)*5:page*5],(page-1)*5+1):
            text+=f"\nФайл {i}: {d['state'] if d['fresh'] else 'Срок хранения истёк'} · {d['size_bytes']} байт · ID {d['id']}"
            if d['state']!='deleted' and d['fresh']:
                buttons.append([(f'Скачать оригинал {i}',f"docopen:{d['id']}")])
                if d['state']=='active':buttons.append([(f'Заменить файл {i}',f"docreplace:{d['id']}")])
        if len(docs)>page*5:buttons.append([('Следующие документы',f"docpage:{ds['id']}:{page+1}")])
        if page>1:buttons.append([('Предыдущие документы',f"docpage:{ds['id']}:{page-1}")])
        if ds['manager']:buttons.append([('Принять',f"docaccept:{ds['id']}")])
        text+=f"\n/review {ds['id']} | request_document / clarify / accept / approve_correction | причина\n/correction {ds['id']} | причина — запросить исправление\n/docdelete ID_файла | причина — запросить удаление\n/docaudit {ds['id']} — история"
        return Reply(text,buttons)

    def _begin_document_upload(self,c,kind,identity,replacement=None):
        ds=self._document_set(c,kind,identity)
        self._workspace_call(c,'SELECT require_open_period(%s)',(ds['occurred_on'],))
        if self._media_queue(c) or self._draft(c) or self._voice_busy(c) or c.execute("SELECT id FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone():return Reply('Сначала завершите текущий ввод или /cancel.')
        c.execute("UPDATE document_uploads SET state='cancelled' WHERE state='pending'")
        c.execute('INSERT INTO document_uploads(workspace_id,author_user_id,set_id,replaces_id) VALUES(current_workspace(),actor_user_id(),%s,%s)',(ds['id'],replacement))
        return Reply(f"Прикрепление к записи {ds['id']} · {self._workspace_name(c)}\nОтправьте JPEG, PNG или PDF до 15 МБ (PDF — до 500 страниц). Файл будет сохранён как документ, новая финансовая операция не создаётся. AI для прикрепления не используется. /cancel — отменить.")

    def _store_document(self,c,ds,data,mime,replacement=None,receipt=None):
        if receipt:
            existing=c.execute('SELECT id,state FROM documents WHERE set_id=%s AND source_receipt_id=%s',(ds['id'],receipt)).fetchone()
            if existing:
                if existing['state']=='deleted':raise ValueError('Исходный документ был удалён. Загрузите файл заново.')
                return existing['id']
        identity=uuid4();permanent=c.execute("SELECT kind='shared' AS yes FROM workspaces WHERE id=current_workspace()").fetchone()['yes']
        sha=hashlib.sha256(data).hexdigest()
        self.document_storage.put(identity,data,permanent)
        try:
            if hashlib.sha256(self.document_storage.read(identity,permanent)).hexdigest()!=sha:raise MediaError('Не удалось проверить сохранённый оригинал.')
            self._workspace_call(c,'SELECT add_document(%s,%s,%s,%s,%s,%s,%s)',(ds['id'],identity,mime,len(data),sha,replacement,receipt))
        except BaseException:
            self.document_storage.remove(identity,permanent);raise
        return identity

    def _attach_source_documents(self,c,operation):
        retention=c.execute("SELECT w.kind,s.keep_personal_originals FROM workspaces w JOIN user_settings s ON s.user_id=actor_user_id() WHERE w.id=current_workspace()").fetchone()
        if retention['kind']=='personal' and not retention['keep_personal_originals']:return
        ds=self._document_set(c,'operation',operation)
        files=c.execute('SELECT f.* FROM operations o JOIN operation_drafts d ON d.id=o.source_draft_id JOIN receipt_batches b ON b.id=d.receipt_batch_id JOIN receipt_files f ON f.batch_id=coalesce(b.parent_batch_id,b.id) WHERE o.id=%s AND NOT EXISTS(SELECT 1 FROM documents doc WHERE doc.set_id=o.id AND doc.source_receipt_id=f.id)',(operation,)).fetchall()
        for f in files:
            try:self._store_document(c,ds,self.receipt_storage.read(f['id']),f['mime_type'],receipt=f['id'])
            except (MediaError,ValueError,OSError):
                # Expense remains real even when attachment persistence fails.
                continue

    def _receive_document(self,telegram_id,bot_id,update_id,data):
        try:prepared=prepare(data,documents_only=True)
        except MediaError as exc:return self.receipt_error(telegram_id,bot_id,update_id,str(exc))
        with self._actor_transaction(telegram_id) as c:
            user=c.execute('SELECT actor_user_id() AS id').fetchone()['id']
            old=c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s',(bot_id,update_id)).fetchone()
            if old:return self._safe_cached(c,old)
            pending=self._document_upload(c)
            if not pending:return Reply('Прикрепление отменено или истекло. Откройте /docs.')
            ds=c.execute('SELECT *,owns_workspace(workspace_id) AS manager FROM document_sets WHERE id=%s',(pending['set_id'],)).fetchone()
            try:
                self._store_document(c,ds,data,prepared.mime,pending['replaces_id'])
                c.execute("UPDATE document_uploads SET state='done' WHERE id=%s",(pending['id'],))
                ds=c.execute('SELECT *,owns_workspace(workspace_id) AS manager FROM document_sets WHERE id=%s',(ds['id'],)).fetchone()
                reply=self._document_card(c,ds);reply.text='Оригинал сохранён и проверен по контрольной сумме.\n'+reply.text
            except (ValueError,OSError):reply=Reply('Файл не добавлен: проверьте квоту, открытый период и доступность хранилища. Можно повторить загрузку; /cancel — отменить.')
            c.execute('INSERT INTO telegram_updates(bot_id,update_id,user_id,response) VALUES(%s,%s,%s,%s)',(bot_id,update_id,user,Jsonb(asdict(reply))))
            return reply

    def _verify_document_files(self,c,identity):
        docs=c.execute("SELECT * FROM documents WHERE set_id=%s AND state='active' AND (expires_at IS NULL OR expires_at>now())",(identity,)).fetchall()
        for d in docs:
            try:raw=self.document_storage.read(d['id'],d['permanent'])
            except OSError:raise ValueError('Оригинал недоступен. Восстановите или замените файл перед принятием.') from None
            if len(raw)!=d['size_bytes'] or hashlib.sha256(raw).hexdigest()!=d['sha256']:raise ValueError('Оригинал повреждён. Замените файл перед принятием.')

    def _resolve_document(self,telegram_id,identity):
        with self._actor_transaction(telegram_id) as c:
            d=c.execute("SELECT * FROM documents WHERE id=%s AND state<>'deleted' AND (expires_at IS NULL OR expires_at>now())",(UUID(identity),)).fetchone()
            if not d:return Reply('Документ недоступен, удалён или срок хранения истёк.')
            try:
                raw=self.document_storage.read(d['id'],d['permanent'])
                if len(raw)!=d['size_bytes'] or hashlib.sha256(raw).hexdigest()!=d['sha256']:raise MediaError('Контрольная сумма оригинала не совпала. Обратитесь в поддержку.')
            except (MediaError,OSError) as exc:return Reply(str(exc) if isinstance(exc,MediaError) else 'Оригинал временно недоступен. Обратитесь в поддержку.')
            ext={'image/jpeg':'jpg','image/png':'png','application/pdf':'pdf'}[d['mime_type']]
            return Reply('Сохранённый оригинал.',generated_document=base64.b64encode(raw).decode(),generated_filename=f'document-{d["id"]}.{ext}')

    def _remove_document_file(self,telegram_id,identity):
        with self._actor_transaction(telegram_id) as c:
            d=c.execute("SELECT * FROM documents WHERE id=%s AND state='deleted'",(UUID(identity),)).fetchone()
            if not d:return Reply('Удаление недоступно.')
            try:
                self.document_storage.remove(d['id'],d['permanent'])
                if d['source_receipt_id']:self.receipt_storage.remove(d['source_receipt_id'])
            except OSError:return Reply('Доступ к документу закрыт. Физическое удаление не завершилось; повторите подтверждение или обратитесь в поддержку.')
            return Reply('Оригинал удалён. Финансовая запись и аудит сохранены.')

    def _documents_callback(self,c,user,callback,sent):
        action,_,raw=callback.partition(':')
        if action=='docpage':
            identity,_,page=raw.partition(':')
            ds=c.execute('SELECT *,owns_workspace(workspace_id) AS manager FROM document_sets WHERE id=%s',(UUID(identity),)).fetchone()
            return self._document_card(c,ds,int(page)) if ds else Reply('Запись недоступна.')
        if action=='docopen':return Reply('Открываю документ.',attachment_id=str(UUID(raw)))
        if action in ('docadd','docview'):
            kind,_,identity=raw.partition(':');identity=UUID(identity)
            return self._begin_document_upload(c,kind,identity) if action=='docadd' else self._document_card(c,self._document_set(c,kind,identity))
        if action=='docreplace':
            d=c.execute("SELECT d.*,ds.entity_kind FROM documents d JOIN document_sets ds ON ds.id=d.set_id WHERE d.id=%s AND d.state='active'",(UUID(raw),)).fetchone()
            return self._begin_document_upload(c,d['entity_kind'],d['set_id'],d['id']) if d else Reply('Документ недоступен.')
        if action=='docaccept':
            self._verify_document_files(c,UUID(raw))
            self._workspace_call(c,'SELECT review_record(%s,%s)',(UUID(raw),'accept'))
            return Reply('Запись принята. Изменение потребует запроса и повторной проверки.')
        if action=='docdeleteok':
            r=c.execute("SELECT * FROM fund_confirmations WHERE id=%s AND expires_at>now()",(UUID(raw),)).fetchone()
            if not r or r['payload'].get('action')!='document_delete':return Reply('Подтверждение недоступно.')
            p=r['payload']
            if r['state']=='pending':
                self._workspace_call(c,'SELECT delete_document(%s,true,%s)',(UUID(p['id']),p['reason']))
                c.execute("UPDATE fund_confirmations SET state='done' WHERE id=%s",(r['id'],))
            if r['state']=='cancelled':return Reply('Удаление отменено.')
            return Reply('Удаление подтверждено.',deleted_attachment_id=p['id'])
        return None

    def _documents_command(self,c,user,command,arg,sent):
        parts=[x.strip() for x in arg.split('|')]
        if command=='/cancel':
            if c.execute("UPDATE document_uploads SET state='cancelled' WHERE state='pending' RETURNING id").fetchone():return Reply('Прикрепление документа отменено.')
            return None
        if command in ('/docs','/attach'):
            bits=arg.split()
            if len(bits) not in (1,2) or not arg:return Reply('/docs operation ID — документы операции; transfer — передачи; claim — заявленного прихода. /attach использует тот же формат.')
            kind=bits[0] if len(bits)==2 else 'operation';identity=UUID(bits[-1])
            return self._begin_document_upload(c,kind,identity) if command=='/attach' else self._document_card(c,self._document_set(c,kind,identity))
        if command in ('/review','/correction'):
            if len(parts)<2:return Reply(command+' ID | '+('действие | причина' if command=='/review' else 'причина'))
            identity=UUID(parts[0]);action=parts[1] if command=='/review' else 'request_correction';reason=parts[2] if len(parts)>2 else (parts[1] if command=='/correction' else None)
            if action=='accept':self._verify_document_files(c,identity)
            self._workspace_call(c,'SELECT review_record(%s,%s,%s)',(identity,action,reason));return Reply('Статус обновлён. /docs ID — карточка.')
        if command=='/reviewqueue':
            filters={}
            if arg:
                for piece in parts:
                    key,sep,value=piece.partition('=')
                    if not sep or key not in ('status','document','member','from','to','category','type','page'):raise ValueError('/reviewqueue status=unreviewed | document=missing | member=Telegram_ID | from=ДД.ММ.ГГГГ | to=ДД.ММ.ГГГГ | category=Категория | type=operation | page=1')
                    filters[key.strip()]=value.strip()
            clauses=["NOT EXISTS(SELECT 1 FROM operations o WHERE o.id=ds.id AND o.state='cancelled')"];args=[]
            for key,column in [('status','ds.status'),('type','ds.entity_kind'),('category','cat.name')]:
                if filters.get(key):clauses.append(column+'=%s');args.append(filters[key])
            if filters.get('member'):clauses.append('m.telegram_user_id=%s');args.append(int(filters['member']))
            if filters.get('document'):
                if filters['document'] not in ('missing','present'):raise ValueError('document=missing либо present')
                clauses.append(('NOT ' if filters['document']=='missing' else '')+"EXISTS(SELECT 1 FROM documents doc WHERE doc.set_id=ds.id AND doc.state='active' AND (doc.expires_at IS NULL OR doc.expires_at>now()))")
            for key,operator in [('from','>='),('to','<=')]:
                if filters.get(key):clauses.append('ds.occurred_on'+operator+'%s');args.append(datetime.strptime(filters[key],'%d.%m.%Y').date())
            page=int(filters.get('page','1'))
            if not 1<=page<=100000:raise ValueError('Номер страницы должен быть положительным.')
            rows=c.execute("SELECT ds.*,m.telegram_user_id FROM document_sets ds LEFT JOIN memberships m ON m.workspace_id=ds.workspace_id AND m.user_id=ds.author_user_id LEFT JOIN categories cat ON cat.id=ds.category_id WHERE "+' AND '.join(clauses)+" ORDER BY ds.occurred_on DESC,ds.id LIMIT 5 OFFSET %s",(*args,(page-1)*5)).fetchall()
            return Reply('Очередь проверки · '+self._workspace_name(c)+'\n'+'\n'.join(f"{i}. {r['occurred_on']} · {money(r['amount'])} · {DOC_STATUS[r['status']]} · автор {r['telegram_user_id'] or 'руководитель'}" for i,r in enumerate(rows,1))+f'\nСтраница {page}; измените page= для перехода. Фильтры: status, document, member, from, to, category, type.',[[(f'Запись {i}',f"docview:{r['entity_kind']}:{r['id']}")] for i,r in enumerate(rows,1)])
        if command=='/docaudit':
            rows=c.execute('SELECT action,reason,created_at FROM review_audit WHERE set_id=%s ORDER BY created_at DESC,id DESC LIMIT 10',(UUID(arg),)).fetchall()
            return Reply('Последние изменения:\n'+'\n'.join(f"{r['created_at']:%d.%m.%Y %H:%M} · {r['action']} · {r['reason'] or '—'}" for r in rows))
        if command in ('/docdelete','/docdeleteconfirm'):
            if len(parts)!=2:return Reply(command+' ID_файла | причина')
            identity=UUID(parts[0])
            if command=='/docdelete':
                self._workspace_call(c,'SELECT delete_document(%s,false,%s)',(identity,parts[1]));return Reply('Запрос удаления сохранён. Руководитель подтверждает через /docdeleteconfirm ID | причина.')
            row=c.execute('INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s) RETURNING id',(Jsonb({'action':'document_delete','id':str(identity),'reason':parts[1]}),)).fetchone()
            return Reply('Удалить оригинал '+str(identity)+'? Причина: '+parts[1]+'. Финансовая запись и аудит останутся.',[[('Удалить оригинал',f"docdeleteok:{row['id']}")]])
        if command in ('/periodclose','/periodopen'):
            if len(parts)!=3:return Reply(command+' ДД.ММ.ГГГГ | ДД.ММ.ГГГГ | причина')
            start,end=(datetime.strptime(x,'%d.%m.%Y').date() for x in parts[:2])
            self._workspace_call(c,'SELECT set_closed_period(%s,%s,%s,%s)',(start,end,command=='/periodclose',parts[2]));return Reply('Период закрыт.' if command=='/periodclose' else 'Период открыт; действие записано в аудит.')
        if command=='/docquota':
            if len(parts)!=2:return Reply('/docquota число_файлов_на_запись | МБ_на_бюджет. Снижение квоты не удаляет сохранённые файлы.')
            self._workspace_call(c,'SELECT document_quota_policy(%s,%s)',(int(parts[0]),int(parts[1])))
            return Reply('Квота обновлена. Уже сохранённые оригиналы остаются доступными.')
        if command=='/docpolicy':
            if not parts or parts[0] not in ('on','off'):return Reply('/docpolicy on/off | минимальная сумма | категория (необязательно). Отсутствие документа не блокирует запись расхода, но требует исключения при принятии.')
            category=None
            if len(parts)>2:
                cat=c.execute('SELECT id FROM categories WHERE lower(name)=lower(%s)',(parts[2],)).fetchone()
                if not cat:raise ValueError('Категория не найдена.')
                category=cat['id']
            self._workspace_call(c,'SELECT document_policy(%s,%s,%s)',(parts[0]=='on',(Decimal(0) if parts[1]=='0' else amount_from_text(parts[1])) if len(parts)>1 else Decimal(0),category));return Reply('Правило документов обновлено.')
        return None
