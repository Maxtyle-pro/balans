"""Personal finance navigation: one balance, no account management screens."""
from balans.domain import Reply,money
from balans.category_settings import CategorySettings

MAIN_BUTTONS=[
 [('➖ Расход','add'),('➕ Доход','ui:go:income')],
 [('📋 История','history'),('📊 Отчёт','report')],
 [('💰 Баланс','balance'),('⚙️ Настройки','ui:go:settings')],
 [('⭐ Подписка','subscription')],
 [('✉️ Написать разработчику','ui:go:contact')]]

HIDDEN_ACTIONS={'sheets','sheets_connect','sheets_off','account','accounts','transfer','exchange','fx','workspaces','workspace','invite','join','members','funds','issue','returnfunds','claim','receive','reconcile','dispute','docs','attach','review','reviewqueue','correction','docaudit','docdelete','docdeleteconfirm','docquota','docquota_set','docpolicy','docpolicy_set','periodclose','periodopen','diagnostic'}

TIMEZONES={'Europe/Moscow':'Москва · UTC+3','Europe/London':'Лондон','Europe/Paris':'Париж','Asia/Dubai':'Дубай · UTC+4','Asia/Yekaterinburg':'Екатеринбург · UTC+5','Asia/Novosibirsk':'Новосибирск · UTC+7','Asia/Vladivostok':'Владивосток · UTC+10','America/New_York':'Нью-Йорк','UTC':'UTC'}

class SimpleInterface(CategorySettings):
    def _ui_menu(self,c,section=''):
        if section in ('preferences','privacy'):return self._simple_settings(c)
        return Reply('💰 Баланс\n\nОтправьте текст, голосовое, фото или документ — я запишу операцию.\nИли выберите действие:',MAIN_BUTTONS)

    def _simple_settings(self,c):
        row=c.execute('SELECT timezone FROM user_settings WHERE user_id=actor_user_id()').fetchone()
        return Reply('⚙️ Настройки\n\nВалюта: '+self._account_context(c)['currency']+'\nЧасовой пояс: '+TIMEZONES.get(row['timezone'],row['timezone'])+'\nЗаписи: '+('сохраняются автоматически' if self._capture_enabled(c) else 'сохраняются после подтверждения'),[
            [('💱 Валюта','currencysettings'),('🕒 Часовой пояс','ui:go:settings_zone')],
            [('💰 Начальный остаток','ui:go:opening')],
            [('🏷 Категории','ui:go:categories')],
            [('🗑 Очистить историю','historyclear')],
            [('Сохранение: '+('автоматически' if self._capture_enabled(c) else 'с подтверждением'),'captureoff' if self._capture_enabled(c) else 'captureon')],
            [('← Главное меню','ui:menu')]])

    def _balance_card(self,c):
        rows=c.execute("SELECT p.currency,sum(p.delta) AS balance FROM postings p JOIN journal_entries e ON e.id=p.entry_id WHERE p.workspace_id=current_workspace() AND e.effective_on<=(now() AT TIME ZONE %s)::date GROUP BY p.currency ORDER BY p.currency",(self._account_context(c)['timezone'],)).fetchall()
        text='💰 Ваш баланс\n\n'+('\n'.join(money(row['balance'],row['currency']) for row in rows) if rows else money(0,self._account_context(c)['currency']))
        return Reply(text+'\n\nПо внесённым доходам, расходам и начальному остатку.',[[('➕ Доход','ui:go:income'),('➖ Расход','add')],[('Начальный остаток','ui:go:opening')],[('← Главное меню','ui:menu')]],command_hints=False)

    def _simple_entry(self,c,user,text,sent,callback):
        category=self._category_settings_entry(c,user,text,callback)
        if category is not None:return category
        if callback=='historyclear':
            row=c.execute("INSERT INTO history_clear_requests(user_id,workspace_id) SELECT %s,id FROM workspaces WHERE owner_user_id=%s AND kind='personal' RETURNING id",(user,user)).fetchone()
            return Reply('⚠️ Очистить всю историю?\n\nБудут удалены все расходы, доходы, начальный остаток, отчёты и загруженные фото, чеки и документы. Файлы будут удалены из хранилища бота.\n\nВосстановить историю в боте нельзя. Подписка, настройки и использованный лимит ИИ сохранятся. Сообщения в Telegram останутся.\n\nСначала скачайте нужные отчёты и файлы.',[[('Отмена','ui:go:settings')],[('🗑 Удалить всю историю',f"historyclearconfirm:{row['id']}")]])
        if callback and callback.startswith('historyclearconfirm:'):
            from uuid import UUID
            c.execute('SELECT clear_personal_history(%s)',(UUID(callback.split(':')[1]),))
            return Reply('✅ История очищена. Баланс обнулён. Загруженные файлы поставлены на удаление.\nМожно записывать новые операции.',MAIN_BUTTONS)
        command=text.split(maxsplit=1)[0].split('@')[0].lower() if text.strip() else ''
        if command=='/notify' or callback and (callback in ('monthlysettings','monthlyon','monthlyoff','ui:go:notify') or callback.startswith('ui:go:notify_')):
            return Reply('🔔 Ежемесячный отчёт, предупреждения о лимите ИИ и сроке хранения файлов приходят автоматически.',[[('☰ Меню','ui:menu')]])
        if (command=='/ai' or command in ('/voice','/receipts') and text.split()[1:]==['off']) or callback and (callback=='ui:section:recognition' or callback in ('ui:go:ai','ui:go:voice','ui:go:receipts','ai_off') or callback in ('ui:go:ai_off','ui:go:voice_off','ui:go:receipts_off')):
            return Reply('Отправьте текст, голосовое, фото или документ — распознавание работает автоматически.',[[('☰ Меню','ui:menu')]])
        if callback=='ui:go:settings_zone':
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            return Reply('🕒 Выберите часовой пояс по городу:',[[(label,'tz:'+zone)] for zone,label in TIMEZONES.items()]+[[('← Настройки','ui:go:settings')]])
        if callback and callback.startswith('tz:'):
            zone=callback[3:]
            if zone not in TIMEZONES:return Reply('Часовой пояс недоступен.')
            c.execute('UPDATE user_settings SET timezone=%s WHERE user_id=%s',(zone,user))
            return self._simple_settings(c)
        if callback=='ui:go:report_period':
            return Reply('📊 За какой период показать отчёт?',[[('Сегодня','period:today'),('Эта неделя','period:week')],[('Этот месяц','period:month'),('Прошлый месяц','period:previous')],[('Выбрать даты','ui:go:report_dates')],[('Главное меню','ui:menu')]])
        if callback and callback.startswith('period:'):
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            choice=callback[7:]
            if choice not in ('today','week','month','previous'):return Reply('Период недоступен.')
            arg=choice
            if choice=='previous':arg=(sent.astimezone(ZoneInfo(self._account_context(c)['timezone'])).date().replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
            return self._dispatch(c,user,'/report '+arg,sent,None)
        if text.split(maxsplit=1) and text.split(maxsplit=1)[0].split('@')[0].lower()=='/sheets' or (callback and (callback in ('ui:go:sheets','ui:go:sheets_connect','ui:go:sheets_off') or callback.split(':')[0] in ('sverify','rsask','rexport'))):
            return Reply('Подключение таблиц отключено. Отчёт можно скачать файлом.',[[('📊 Отчёт','report')],[('Главное меню','ui:menu')]])
        handled=callback in ('ui:section:recognition','balance','accounts','ui:go:accounts','ui:go:settings','add','ui:go:add') or bool(callback and callback.startswith('ui:go:') and callback[6:] in HIDDEN_ACTIONS)
        if handled:c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
        if callback=='workspaces':return self._ui_menu(c)
        if callback=='ui:go:privacy':
            return Reply('🔒 Приватность\n\nВаши личные записи доступны только вам. Для распознавания сообщения и файлы передаются в OpenAI. Голосовые файлы не сохраняются. Фото и документы хранятся 90 дней; перед удалением предложим продление. Продление оплачивается только после вашего согласия.',[[('📎 Мои файлы','ui:go:files')],[('Удалить мои данные','ui:go:delete')],[('Поддержка','ui:go:support')],[('← Настройки','ui:go:settings')]])
        if callback=='ui:section:recognition':
            return Reply('🤖 Распознавание\nВыберите, что настроить:',[[('Текст','ui:go:ai'),('Голос','ui:go:voice'),('Фото и документы','ui:go:receipts')],[('← Настройки','ui:go:settings')]])
        if callback in ('balance','accounts','ui:go:accounts'):return self._balance_card(c)
        if callback=='ui:go:settings':return self._simple_settings(c)
        if callback in ('add','ui:go:add'):
            if self._media_queue(c):return self._media_blocker(c)
            if self._draft(c):return self._ui_resume(c,user,sent)
            return Reply('➖ Добавить расход\n\nНапишите, на что и сколько потратили: «Вода 339».\nМожно отправить голосовое, фото чека или документ.',[[('Ввести вручную','ui:go:manual')],[('← Главное меню','ui:menu')]])
        if callback and (callback.startswith('acuse:') or callback.startswith('ffield:account:') or callback.startswith('mf:account:')):
            return Reply('Все операции учитываются в одном балансе.',[[('Главное меню','ui:menu')]])
        if callback and callback.startswith('ui:go:') and callback[6:] in HIDDEN_ACTIONS:
            return Reply('Учёт стал проще: доступны доходы, расходы и один баланс.',MAIN_BUTTONS)
        return None
