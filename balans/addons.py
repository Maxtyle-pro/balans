"""Explicit demo add-ons and database-authorized retention, with no real charges."""
import base64
import io
import zipfile
from uuid import UUID
from datetime import timedelta
from balans.domain import Reply
from balans.onboarding import DEMO_BADGE

QUOTA_NAMES={'text':'ИИ-распознавания текста','image':'Распознавание изображений','voice':'Голосовой ввод (секунды)','analysis':'ИИ-анализ отчётов'}
PACKS={'text':(100,25),'image':(50,50),'voice':(1800,50),'analysis':(10,50),'upgrade':(1,100),'storage':(90,25)}

class Addons:
    def _addons_available(self,c,storage=False):
        if not storage:return bool(self._demo_state(c))
        return bool(c.execute('SELECT 1 FROM billing_demo d JOIN workspaces w ON w.id=current_workspace() WHERE d.user_id=actor_user_id() AND d.allowed AND d.enabled AND w.owner_user_id=actor_user_id()').fetchone())

    def _quota_card(self,c,kind,exhausted=True):
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        usage=c.execute('SELECT billing_usage() AS data').fetchone()['data']
        maximum=(access.get('quotas') or {}).get(kind,0);used=usage.get(kind,0)
        text=('🤖 Лимит ИИ исчерпан.' if exhausted else '🤖 Осталось не больше 20% лимита ИИ.')+f'\n{QUOTA_NAMES[kind]}: {used} из {maximum}.'
        if access.get('period_end'):text+='\nЛимит обновится: '+self._local_deadline(c,access['period_end'])+'.'
        text+='\nРучной ввод остаётся доступен. Ошибки обработки не расходуют лимит.'
        buttons=[[('Добавить пакет',f'addon:{kind}')],[('Выбрать тариф','addon:upgrade')]] if self._addons_available(c) else [[('Моя подписка','subscription')]]
        return Reply(text,buttons+[[('Ввести вручную','ui:go:manual')]])

    def _quota_preflight(self,c,kind,quantity=1):
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if access['status'] not in ('trial','active'):return None
        usage=c.execute('SELECT billing_usage() AS data').fetchone()['data']
        if usage.get(kind,0)+quantity>(access.get('quotas') or {}).get(kind,0):return self._quota_card(c,kind)
        return None

    def _addon_offer(self,c,kind,batch=None):
        demo=(c.execute('SELECT d.* FROM billing_demo d JOIN workspaces w ON w.id=current_workspace() WHERE d.user_id=actor_user_id() AND d.allowed AND d.enabled AND w.owner_user_id=actor_user_id()').fetchone() if kind=='storage' else self._demo_state(c))
        if not demo:return Reply('Дополнительные пакеты и расширенный тариф пока доступны для тестирования оплаты. Реальные цены ещё не утверждены.',[[('Моя подписка','subscription'),('Меню','ui:menu')]])
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if kind!='storage' and access['status'] not in ('trial','active'):return self._subscription_card(c)
        units,stars=PACKS[kind]
        if kind=='storage':
            if c.execute("SELECT 1 FROM workspaces w JOIN user_settings s ON s.user_id=w.owner_user_id WHERE w.id=current_workspace() AND w.kind='personal' AND NOT s.keep_personal_originals").fetchone():
                return Reply('Хранение личных оригиналов выключено. Сначала включите его, затем выберите продление.',[[('Включить хранение','ui:go:retention_on')]])
            row=c.execute('SELECT * FROM retention_batches WHERE id=%s AND user_id=actor_user_id() AND workspace_id=current_workspace() AND extended_at IS NULL',(batch,)).fetchone()
            if not row:return Reply('Файлы недоступны или срок уже продлён.',[[('Меню','ui:menu')]])
            deadline=max(row['deadline'],c.execute('SELECT now() AS t').fetchone()['t'])+timedelta(days=90)
            description=f"Продлить хранение {len({f.get('sha',f['id']) for f in row['files']})} файлов на 90 дней. Новая дата — не раньше {self._local_deadline(c,deadline)}."
        elif kind=='upgrade':
            description='Расширенный тариф: вдвое больше базовых квот до конца текущего периода. Доплата сейчас — условно 100 Stars. Следующий период — базовый тариф по прежней цене; автоматического продления расширенного тарифа пока нет.'
        else:description=f'{QUOTA_NAMES[kind]}: дополнительно {units}. Пакет действует до конца текущего периода; остаток не переносится.'
        if kind!='storage':description+='\nСрок: '+self._local_deadline(c,access['period_end'])+'.'
        order=c.execute('INSERT INTO addon_orders(user_id,workspace_id,product,stars,units,batch_id,demo_generation,period_start,period_end) VALUES(actor_user_id(),current_workspace(),%s,%s,%s,%s,%s,%s,%s) RETURNING id',(kind,stars,units,batch,demo['generation'],access.get('period_start'),access.get('period_end'))).fetchone()['id']
        return Reply(DEMO_BADGE+description+f'\n\nУсловная цена: {stars} Stars. Реального списания не будет.',[[('Оплатить тестово',f'addonpay:{order}')],[('Отмена','ui:menu')]])

    def _retention_card(self,c,batch):
        row=c.execute('SELECT * FROM retention_batches WHERE id=%s AND user_id=actor_user_id() AND workspace_id=current_workspace()',(batch,)).fetchone()
        if not row:return Reply('Файлы недоступны.',[[('Меню','ui:menu')]])
        if row['extended_at']:return Reply('✅ Хранение этих файлов уже продлено на 90 дней.',[[('Меню','ui:menu')]])
        deadline=max(row['deadline'],row['notice_sent_at']+timedelta(days=7)) if row['notice_sent_at'] else row['deadline']
        demo=self._addons_available(c,storage=True)
        text=f"📎 Файлов: {len({f.get('sha',f['id']) for f in row['files']})}.\nСрок хранения: {self._local_deadline(c,deadline)}.\n\nСкачайте архив до удаления. Операции, категории и отчёты останутся."
        if demo:text+='\nПродление на 90 дней доступно в тестовом режиме.'
        return Reply(text,([[('Продлить на 90 дней',f'storagebuy:{batch}')]] if demo else [])+[[('Скачать архив',f'storagezip:{batch}')],[('Удалить в срок',f'storageskip:{batch}')]])

    def _retention_archive(self,c,batch,part=None):
        row=c.execute('SELECT * FROM retention_batches WHERE id=%s AND user_id=actor_user_id() AND workspace_id=current_workspace()',(batch,)).fetchone()
        if not row:return Reply('Архив недоступен.')
        chunks=[[]];size=0
        seen=set()
        for f in sorted(row['files'],key=lambda f:f['kind']=='receipt'):
            if f.get('sha',f['id']) in seen:continue
            seen.add(f.get('sha',f['id']))
            if size+f['size']>35*1024*1024:chunks.append([]);size=0
            chunks[-1].append(f);size+=f['size']
        if part is None and len(chunks)>1:return Reply('Архив разделён на части до 35 МБ.',[[(f'Скачать часть {i+1}',f'storagezip:{batch}:{i}')] for i in range(len(chunks))])
        index=part or 0
        if not 0<=index<len(chunks):return Reply('Часть архива недоступна.')
        buffer=io.BytesIO();missing=0
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_STORED) as archive:
            for f in chunks[index]:
                # Recheck current access/deletion state; cached batch metadata grants no file access.
                table='receipt_files' if f['kind']=='receipt' else 'documents'
                valid=c.execute(f'SELECT id FROM {table} WHERE id=%s AND expires_at>now()'+(" AND telegram_file_id<>''" if table=='receipt_files' else " AND state<>'deleted'"),(UUID(f['id']),)).fetchone()
                if not valid:missing+=1;continue
                try:
                    data=self.receipt_storage.read(f['id']) if f['kind']=='receipt' else self.document_storage.read(f['id'],f['kind']=='shared')
                except (ValueError,OSError):missing+=1;continue
                extension={'application/pdf':'.pdf','image/png':'.png','image/jpeg':'.jpg'}[f['mime']]
                archive.writestr(f['id']+extension,data)
        return Reply('📦 Архив файлов.'+(f' Недоступных файлов: {missing}.' if missing else ''),generated_document=base64.b64encode(buffer.getvalue()).decode(),generated_filename=f'files-{index+1}.zip')

    def _storage_overview(self,c):
        rows=c.execute("SELECT id,deadline,extended_at FROM retention_batches WHERE user_id=actor_user_id() AND workspace_id=current_workspace() AND extended_at IS NULL ORDER BY deadline LIMIT 20").fetchall()
        return Reply('📎 Базовый срок хранения фото, чеков и документов — 90 дней.\nПеред удалением предложим скачать архив. Операции и отчёты сохранятся.'+('\n\nВыберите подборку файлов:' if rows else '\n\nСейчас нет файлов, срок хранения которых подходит к концу.'),[[(f"Файлы до {r['deadline']:%d.%m.%Y}",f"storage:{r['id']}")] for r in rows]+[[('Меню','ui:menu')]])

    def _addon_callback(self,c,user,callback):
        if callback=='addonmenu' and not self._addons_available(c):return self._addon_offer(c,'text')
        if callback=='addonmenu':return Reply('Выберите дополнительный пакет. Цена и срок будут показаны перед подтверждением.',[[(label,'addon:'+kind)] for kind,label in QUOTA_NAMES.items()]+[[('Назад','subscription')]])
        if callback.startswith('addon:'):
            kind=callback.split(':')[1]
            return self._addon_offer(c,kind) if kind in PACKS and kind!='storage' else Reply('Пакет недоступен.')
        if callback.startswith('addonpay:'):
            paid=c.execute('SELECT confirm_demo_addon(%s) AS paid',(UUID(callback.split(':')[1]),)).fetchone()['paid']
            return Reply(DEMO_BADGE+('✅ Покупка подтверждена. Дополнительный объём доступен.' if paid else 'Эта покупка уже обработана. Повторного начисления нет.'),[[('Моя подписка','subscription'),('Меню','ui:menu')]])
        if callback.startswith(('storagebuy:','storagezip:','storageskip:','storage:')):
            parts=callback.split(':');batch=UUID(parts[1])
            if parts[0]=='storagebuy':return self._addon_offer(c,'storage',batch)
            if parts[0]=='storagezip':return self._retention_archive(c,batch,int(parts[2]) if len(parts)>2 else None)
            if parts[0]=='storageskip':
                row=c.execute('SELECT id FROM retention_batches WHERE id=%s AND user_id=actor_user_id()',(batch,)).fetchone()
                if not row:return Reply('Список недоступен.')
                return Reply('Файлы будут удалены в срок. До этой даты вы можете скачать их или продлить хранение.',[[('Вернуться к файлам',f'storage:{batch}')]])
            return self._retention_card(c,batch)
        return None
