"""Button navigation over existing commands; free input contains arguments only."""
from dataclasses import dataclass, replace
import re
from uuid import UUID
from balans.domain import Reply
from balans.history_ui import HistoryUI


@dataclass(frozen=True)
class Action:
    label: str
    command: str
    prompt: str = ''


# Callback payloads use this allowlist, never arbitrary commands or user text.
ACTIONS = {}
def action(key,label,command=None,prompt=''):
    ACTIONS[key]=Action(label,command or '/'+key,prompt)

for key,label in [
    ('start','🏠 Главная'),('help','☰ Все действия'),('add','➕ Добавить расход'),('manual','Ввести расход вручную'),
    ('income','Добавить доход'),('opening','Начальный остаток'),('history','🕘 История'),('report','📊 Отчёт'),
    ('pdf','Отчёт PDF'),('analyze','✨ ИИ-анализ'),('csv','Экспорт CSV'),('accounts','Счета и остатки'),
    ('workspaces','Мои бюджеты'),('members','Участники'),('invite','Пригласить участника'),('funds','Выдачи и сверка'),
    ('reviewqueue','Очередь проверки'),('budget','Лимиты расходов'),('notify','Уведомления'),('settings','Настройки'),
    ('categories','Категории'),('rules','Мои правила'),('category','Изменить категорию'),('ai','Настройки ИИ'),
    ('receipts','Чеки'),('voice','Голосовой ввод'),('batch','Список операций'),('media','Продолжить список'),
    ('subscription','⭐ Моя подписка'),('renewal','Автопродление'),('terms','Условия тарифа'),
    ('privacy','Приватность'),('retention','Хранение оригиналов'),('delete','Удалить профиль'),
    ('support','Поддержка'),('paysupport','Поддержка оплаты'),('cancel','Отменить текущий ввод'),
    ('sheets','Google Sheets'),('docquota','Квоты документов'),('docpolicy','Правила документов'),
    ('admin','Панель администратора'),('demo','Тестовый пульт')]:action(key,label)

for key,label,prompt in [
    ('search','Найти операцию','Что найти в описаниях покупок? Например: кофе.'),
    ('account','Создать счёт','Введите название счёта. Например: Наличные. Для другой валюты: Доллары | USD.'),
    ('workspace','Создать общий бюджет','Как назвать общий бюджет? Например: Семья.'),
    ('join','Вступить в бюджет','Пришлите код приглашения, полученный от владельца бюджета.'),
    ('transfer','Перевести между счетами','Введите сумму и счёт получателя через «|». Например: 1000 | Наличные | сегодня | На покупки.'),
    ('exchange','Обменять валюту','Введите сумму списания, счёт получателя, сумму зачисления и дату через «|». Например: 1000 | Доллары | 10 | сегодня.'),
    ('fx','Задать курс','Введите валюту, курс к базовой валюте бюджета и дату через «|». Например: USD | 95,50 | сегодня.'),
    ('rule','Запомнить правило','Введите слово и категорию через «|». Например: кофе | Кафе и рестораны.'),
    ('issue','Выдать средства','Введите Telegram ID получателя, сумму, дату и назначение через «|». Например: 123456 | 5000 | сегодня | Покупки.'),
    ('returnfunds','Вернуть средства','Введите сумму, дату и назначение через «|». Например: 1000 | сегодня | Остаток.'),
    ('claim','Заявить приход','Введите сумму, дату, источник и назначение через «|». Например: 5000 | сегодня | Руководитель | Покупки.'),
    ('receive','Подтвердить получение','Введите ID передачи и полученную сумму через «|». ID показан в разделе «Выдачи и сверка».'),
    ('reconcile','Сверить приход','Введите ID прихода, действие и причину через «|». Например: ID | external | Сумма проверена. Действия: external (внешний приход), rejected (отклонить), matched (связать с выдачей; добавьте её ID четвёртым полем).'),
    ('dispute','Сообщить расхождение','Введите ID передачи и причину через «|». ID показан в разделе «Выдачи и сверка».'),
    ('docs','Документы записи','Введите ID операции. Для передачи или прихода: transfer ID или claim ID. Также документы можно открыть из истории.'),
    ('attach','Прикрепить документ','Введите ID операции, к которой прикрепить документ. Затем бот попросит файл.'),
    ('review','Проверить запись','Введите ID набора документов, действие и причину через «|». Действия: request_document, clarify, accept, approve_correction.'),
    ('correction','Запросить исправление','Введите ID набора документов и причину через «|».'),
    ('periodclose','Закрыть период','Введите начало, конец периода и причину через «|». Например: 01.08.2026 | 31.08.2026 | Проверено.'),
    ('periodopen','Открыть период','Введите начало, конец периода и причину через «|». Например: 01.08.2026 | 31.08.2026 | Исправление.'),
    ('diagnostic','Передать данные поддержке','Введите ID операции для диагностики. Перед передачей бот запросит подтверждение.'),
    ('docaudit','История документа','Введите ID набора документов.'),
    ('docdelete','Удалить документ','Введите ID документа и причину через «|». Это создаст запрос удаления для руководителя.'),
    ('budgetday','Начало бюджетного месяца','С какого числа считать бюджетный месяц? Введите число от 1 до 28.')]:action(key,label,prompt=prompt)

for key,label,command,prompt in [
    ('media_cancel','Отменить список','/media cancel',''),('batch_cancel','Отменить оставшееся','/batch cancel',''),
    ('report_period','Выбрать период','/report','За какой период нужен отчёт? Например: август — в формате 2026-08, либо 01.08.2026 15.08.2026.'),
    ('budget_set','Установить лимит','/budget','Введите сумму лимита. Для категории: 10000 | Продукты.'),
    ('settings_zone','Изменить часовой пояс','/settings','Введите часовой пояс. Например: Europe/Moscow или Asia/Yekaterinburg.'),
    ('categories_add','Добавить категорию','/categories add','Как назвать категорию?'),
    ('categories_rename','Переименовать категорию','/categories rename','Введите прежнее и новое названия через «|».'),
    ('categories_archive','Скрыть категорию','/categories archive','Введите название категории, которую нужно скрыть.'),
    ('sheets_connect','Подключить таблицу','/sheets connect','Пришлите ссылку на свою Google-таблицу.'),
    ('notify_time','Время уведомлений','/notify time','В какое время присылать уведомления? Например: 19:00.'),
    ('notify_quiet','Тихие часы','/notify quiet','Введите начало и конец тихих часов через пробел. Например: 22:00 09:00.'),
    ('docquota_set','Изменить квоты документов','/docquota','Введите лимиты по формату: число файлов | мегабайты.'),
    ('docpolicy_set','Изменить правила документов','/docpolicy','Введите правило: on или off. Для порога и категории: on | 10000 | Продукты.')]:action(key,label,command,prompt)
for key,label,command in [
    ('ai_on','Включить ИИ','/ai on'),('ai_off','Выключить ИИ','/ai off'),
    ('receipts_on','Включить чеки','/receipts on'),('receipts_off','Выключить чеки','/receipts off'),
    ('voice_on','Включить голос','/voice on'),('voice_off','Выключить голос','/voice off'),
    ('retention_on','Хранить оригиналы','/retention on'),('retention_off','Не хранить оригиналы','/retention off'),
    ('notify_on','Включить уведомления','/notify on'),('notify_off','Выключить уведомления','/notify off'),
    ('notify_weekly','Включить недельный отчёт','/notify weekly on'),('notify_weekly_off','Выключить недельный отчёт','/notify weekly off'),
    ('notify_reminder','Включить напоминания','/notify reminder on'),('notify_reminder_off','Выключить напоминания','/notify reminder off'),
    ('notify_budget','Включить уведомления о лимитах','/notify budget on'),('notify_budget_off','Выключить уведомления о лимитах','/notify budget off'),
    ('notify_monthly','Включить месячный отчёт','/notify monthly on'),('notify_monthly_off','Выключить месячный отчёт','/notify monthly off'),
    ('sheets_off','Отключить таблицу','/sheets off')]:action(key,label,command)

SECTIONS={
 'records':('📝 Записи',['add','income','manual','history','search','media','batch','cancel']),
 'reports':('📊 Отчёты',['report','report_period','pdf','analyze','csv','sheets','sheets_connect','sheets_off']),
 'money':('💳 Счета и бюджеты',['accounts','account','opening','transfer','exchange','fx','budget','budget_set','budgetday']),
 'shared':('👥 Общий бюджет',['workspaces','workspace','invite','join','members','funds','issue','returnfunds','claim','receive','reconcile','dispute']),
 'documents':('📎 Документы',['reviewqueue','docs','attach','review','correction','docaudit','docdelete','docquota','docquota_set','docpolicy','docpolicy_set','periodclose','periodopen']),
 'preferences':('⚙️ Настройки',['settings','settings_zone','categories','categories_add','categories_rename','categories_archive','rules','rule','category','ai','receipts','voice','notify']),
 'subscription':('⭐ Подписка',['subscription','renewal','terms','paysupport']),
 'privacy':('🔒 Приватность и помощь',['privacy','retention','delete','support','diagnostic'])}
EXTRAS={'budget':['budget_set','budgetday'],'settings':['settings_zone'],'categories':['categories_add','categories_rename','categories_archive'],
 'notify':['notify_on','notify_off','notify_monthly','notify_monthly_off','notify_weekly','notify_weekly_off','notify_reminder','notify_reminder_off','notify_budget','notify_budget_off','notify_time','notify_quiet'],
 'ai':['ai_on','ai_off'],'voice':['voice_on','voice_off'],'receipts':['receipts_on','receipts_off'],
 'retention':['retention_on','retention_off'],'sheets':['sheets_connect','sheets_off'],'docquota':['docquota_set'],'docpolicy':['docpolicy_set']}


action('docdeleteconfirm','Подтвердить удаление документа',prompt='Введите ID документа и причину через «|». Затем подтвердите удаление кнопкой.')
SECTIONS['documents'][1].append('docdeleteconfirm')

def buttons(keys):return [[(ACTIONS[key].label,'ui:go:'+key)] for key in dict.fromkeys(keys)]


COMMAND_PATTERN=re.compile(r'(?<![\w/])/(?P<name>[a-z][a-z0-9]*)(?!\w)')

def present_reply(reply):
    """Offer only allowlisted buttons. Never execute or rewrite text from records."""
    found=[]
    for match in COMMAND_PATTERN.finditer(reply.text):
        key=match['name']
        if key not in ACTIONS:continue
        tail=reply.text[match.start():]
        presets=[k for k,a in ACTIONS.items() if ' ' in a.command and not a.prompt and
                 tail.startswith(a.command) and (len(tail)==len(a.command) or not tail[len(a.command)].isalnum())]
        key=max(presets,key=lambda k:len(ACTIONS[k].command)) if presets else key
        found.append(key)
        if match['name']=='history':
            page=re.match(r'/history ([0-9]{1,6})(?!\d)',tail)
            if page and int(page[1])>0:found[-1]='history:'+page[1]
    existing={data for row in reply.buttons for _,data in row}
    rows=[list(row) for row in reply.buttons]
    for key in dict.fromkeys(found):
        data='ui:go:'+key
        if data in existing or key in existing:continue
        if len(rows)>=80:break
        label='История · страница '+key.split(':')[1] if key.startswith('history:') else ACTIONS[key].label
        rows.append([(label,data)]);existing.add(data)
    if found and 'ui:menu' not in existing and 'ui:go:help' not in existing:rows.append([('☰ Все действия','ui:menu')])
    return replace(reply,buttons=rows)


class CommandUI(HistoryUI):
    def _ui_menu(self,c,section=''):
        if section:
            if section not in SECTIONS:return Reply('Раздел недоступен.',[[('☰ Все действия','ui:menu')]])
            title,keys=SECTIONS[section]
            return Reply(title+'\nВыберите действие:',buttons(keys)+[[('← Все разделы','ui:menu')]])
        rows=[[(title,'ui:section:'+key)] for key,(title,_) in SECTIONS.items()]
        if self._demo_enabled(c):rows.append([('🧪 Тестовый пульт','demopanel')])
        if c.execute('SELECT admin_role() AS role').fetchone()['role']:rows.append([('Панель администратора','ui:go:admin')])
        return Reply('☰ Что хотите сделать?\nВыберите раздел — все действия доступны кнопками.',rows+[[('🏠 Главная','start')]])

    def _ui_resume(self,c,user,sent):
        q=self._media_queue(c)
        if q:return self._media_list(c,q)
        draft=self._draft(c)
        if draft:return self._prompt(c,draft)
        batch=self._input_batch(c)
        if batch:return self._batch_card(c,batch)
        if self._voice_busy(c):return self._voice_command(c,user,'/voice','')
        if self._document_upload(c):return Reply('Прикрепление документа ожидает файл. Пришлите фото или PDF.',buttons(['cancel']))
        receipt=c.execute("SELECT * FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone()
        if receipt:return self._receipt_command(c,user,'/receipts','')
        return self._ui_menu(c)

    def _ui_entry(self,c,user,text,sent,callback):
        history=self._history_entry(c,user,text,sent,callback)
        if history is not None:return history
        if callback and callback.startswith('ui:inputcancel:'):
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s AND id=%s',(user,UUID(callback.split(':')[-1])))
            return self._ui_menu(c)
        if callback and callback.startswith('ui:'):
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            if callback=='ui:menu':return self._ui_menu(c)
            if callback=='ui:resume':return self._ui_resume(c,user,sent)
            if callback.startswith('ui:section:'):return self._ui_menu(c,callback.split(':',2)[2])
            if not callback.startswith('ui:go:'):return Reply('Кнопка устарела.',[[('☰ Все действия','ui:menu')]])
            key=callback[6:]
            if re.fullmatch(r'history:[1-9][0-9]{0,5}',key):return self._dispatch(c,user,'/history '+key.split(':')[1],sent,None)
            if key not in ACTIONS:return Reply('Действие недоступно.',[[('☰ Все действия','ui:menu')]])
            if key=='media_cancel':return self._media_blocker(c)
            item=ACTIONS[key]
            if item.prompt:
                if self._workspace_busy(c) or self._input_batch(c):
                    return Reply('Сначала завершите текущий ввод или отмените его.',[[('Продолжить','ui:resume'),('Отменить ввод','ui:go:cancel')]])
                row=c.execute("INSERT INTO ui_inputs(user_id,workspace_id,action) VALUES(%s,current_workspace(),%s) RETURNING id",(user,key)).fetchone()
                return Reply(item.label+'\n\n'+item.prompt,[[('Отмена',f"ui:inputcancel:{row['id']}")]])
            reply=self._dispatch(c,user,item.command,sent,None)
            reply.buttons+=buttons(EXTRAS.get(key,[]))
            return reply
        if callback or text.startswith('/'):
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            return None
        pending=c.execute('SELECT *,expires_at>now() AS fresh,workspace_id=current_workspace() AS same_workspace FROM ui_inputs WHERE user_id=%s',(user,)).fetchone()
        if not pending:return None
        if not pending['fresh'] or not pending['same_workspace'] or pending['action'] not in ACTIONS:
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            return Reply('Ввод устарел. Выберите действие заново.',[[('☰ Все действия','ui:menu')]])
        if not text.strip():return Reply(ACTIONS[pending['action']].prompt)
        c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
        reply=self._dispatch(c,user,ACTIONS[pending['action']].command+' '+text.strip(),sent,None)
        reply.buttons+=buttons([pending['action']])
        return reply
