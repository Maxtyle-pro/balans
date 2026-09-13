from balans.simple_interface import SimpleInterface,MAIN_BUTTONS
from balans.text_recognition import TextRecognition
from balans.text_ai import TextAI
"""Durable conversation: each update and its response commit atomically."""
from balans.command_ui import CommandUI
from balans.addons import Addons
from balans.automatic_capture import AutomaticCapture
from dataclasses import asdict
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from balans.domain import Reply, amount_from_text, date_from_text, money, CURRENCY
from balans.ai import CategoryAI
from balans.categorization import Categorization
from balans.receipts import Receipts
from balans.workspaces import Workspaces
from balans.funds import Funds
from balans.documents import Documents,DocumentStorage
from balans.media_flow import MediaFlow
from balans.sharing import Sharing
from balans.shared_reports import SharedReports
from balans.planning import Planning
from balans.billing import Billing
from balans.inbox import Inbox
from balans.refunds import Refunds
from balans.admin_auth import AdminAuth,AdminConfig
from balans.admin_service import AdminService
from balans.privacy import Privacy
from balans.currency_flow import CurrencyFlow
from psycopg.errors import RaiseException
from balans.notifications import Notifications
from balans.input_flow import InputFlow
from balans.finance import Finance, KINDS
from balans.reports import Reports
from balans.report_ai import ReportAI
from balans.sheets import Sheets
from balans.voice import Voice
from balans.voice_ai import VoiceAI
from balans.receipt_ai import ReceiptAI
from balans.receipt_media import ReceiptStorage

HELP = ('/subscription — подписка и оплата\n/diagnostic — передать снимок поддержке\n/budget — лимиты расходов\n/notify — уведомления\nБаланс — личный учёт расходов.\n\n'
        '/add — добавить расход пошагово\n/add 850 — начать с суммы\n'
        '/history — последние расходы (страницы: /history 2)\n'
        '/report — отчёт за месяц или выбранный период\n/pdf — PDF с графиками\n/analyze — AI-анализ\n/csv — экспорт CSV\n/sheets — подключение Google Sheets\n/accounts — счёт и учётный остаток\n'
        '/media — строки из изображений\n'
        '/reviewqueue — очередь проверки\n/docquota — квоты документов\n'
        '/docs — документы записи\n/attach — прикрепить документ\n/review — проверить запись\n/correction — запрос исправления\n/docpolicy — обязательные документы\n/periodclose — закрыть период\n/periodopen — открыть период\n'
        '/funds — выдачи и сверка\n/issue — выдать средства\n/returnfunds — вернуть руководителю\n/claim — заявить приход\n/receive — подтвердить получение\n/reconcile — сверить приход\n/dispute — расхождение\n'
        '/workspaces — выбор бюджета\n/workspace — создать совместный бюджет\n/invite — пригласить\n/join — вступить\n/members — участники\n'
        '/batch — список из нескольких операций\n/search — поиск по описанию\n/categories — свои категории\n'
        '/income — доход\n/account — создать счёт\n/opening — начальный остаток\n/transfer — перевод между счетами\n'
        '/settings — настройки и часовой пояс\n/cancel — отменить черновик\n'
        '/ai — включить AI-категоризацию\n/category — исправить категорию черновика\n'
        '/rules — личные правила\n/rule — правило по словам\n/manual — ручной ввод\n'
        '/receipts — фото и PDF-чек\n/voice — голосовой ввод\n'
        '/support — поддержка\n/help — помощь\n\n'
        'Можно отправить сообщение без команды. Распознанные операции записываются автоматически; исправления — кнопкой «Изменить». '
        'Доступны личные и совместные бюджеты, RUB, USD и EUR. /privacy — приватность; /delete — удаление профиля; /exchange — обмен; /fx — ручной курс.')
QUICK_HELP = ('💡 Как пользоваться Балансом\n\n'
              '✍️ Напишите «Кофе 250» или «Зарплата 50000».\n'
              '🎙 Отправьте голосовое сообщение или фото чека.\n'
              '✅ Операция запишется автоматически. При необходимости нажмите «Изменить».\n\n'
              '📊 История, отчёты, баланс и настройки — в меню «Все действия».')
MENU = MAIN_BUTTONS


class Service(SimpleInterface, TextRecognition, AutomaticCapture, Addons, CommandUI, CurrencyFlow, Privacy, AdminAuth, AdminService, Refunds, Inbox, Billing, Planning, Notifications, SharedReports, Sharing, MediaFlow, Documents, Funds, Workspaces, InputFlow, Finance, Reports, Voice, Receipts, Categorization):
    def __init__(self, dsn: str, support: str = '', ai: CategoryAI | None = None, receipt_ai=None, receipt_storage=None, voice_ai=None, report_ai=None, sheets=None, admin_config=None):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True, kwargs={'row_factory': dict_row})
        self.admin_config=admin_config or AdminConfig.from_env()
        self.support = support
        self.ai = ai or CategoryAI()
        self.text_ai = TextAI(getattr(self.ai,'client',None),self.ai.model)
        self.receipt_ai = receipt_ai or ReceiptAI(getattr(self.ai,'client',None),self.ai.model)
        self.report_ai = report_ai or ReportAI(getattr(self.ai,'client',None),self.ai.model)
        self.sheets = sheets or Sheets()
        self.voice_ai = voice_ai or VoiceAI(getattr(self.ai,'client',None),self.ai.model)
        self.receipt_storage = receipt_storage or ReceiptStorage()
        self.document_storage = DocumentStorage(self.receipt_storage.root.parent/'documents')

    def close(self):
        self.pool.close()
        self.ai.close()
        self.sheets.close()

    def check(self):
        with self.pool.connection() as c:
            role = c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            if role['rolsuper'] or role['rolbypassrls']:
                raise RuntimeError('DATABASE_URL должен использовать runtime-роль без SUPERUSER/BYPASSRLS')
            unsafe = c.execute("SELECT has_table_privilege(current_user,'balans.postings','INSERT,UPDATE,DELETE') AS unsafe").fetchone()
            if unsafe['unsafe']:
                raise RuntimeError('Runtime-роль не должна напрямую изменять журнал')
            c.execute('SELECT lease_token FROM balans.telegram_inbox LIMIT 0')
            c.execute('SELECT 1 FROM balans.currencies LIMIT 1')
            config=c.execute('SELECT * FROM balans.ai_configuration').fetchone()
            if config['model']:
                for adapter in (self.ai,self.text_ai,self.receipt_ai,self.report_ai,self.voice_ai):adapter.model=config['model']
            if config['transcribe_model']:self.voice_ai.transcribe_model=config['transcribe_model']
            if config['confidence'] is not None:self.ai.threshold=float(config['confidence'])

    def _safe_cached(self,c,row):
        workspace=row.get('workspace_id')
        if not workspace:return Reply('Это событие уже обработано. /history — сохранённые операции; /add — текущий ввод.')
        if not c.execute('SELECT id FROM workspaces WHERE id=%s',(workspace,)).fetchone():return Reply('Доступ к бюджету прекращён. Данные недоступны.')
        return Reply(**row['response'])

    def _handle_impl(self, telegram_id: int, bot_id: int, update_id: int, text: str,
               sent_at: datetime, callback: str | None = None) -> Reply:
        with self._actor_transaction(telegram_id) as c:
            user_id = c.execute('SELECT bootstrap() AS id').fetchone()['id']
            c.execute('SELECT record_activity()')
            c.execute('UPDATE notification_preferences SET blocked=false WHERE user_id=%s AND blocked',(user_id,))
            old = c.execute('SELECT response,workspace_id FROM telegram_updates WHERE bot_id=%s AND update_id=%s', (bot_id, update_id)).fetchone()
            if old:
                reply = self._safe_cached(c,old)
            else:
                c.execute("UPDATE operation_drafts SET state='cancelled' WHERE state='pending' AND expires_at<=now()")
                try:
                    with c.transaction():reply = self._dispatch(c, user_id, text.strip(), sent_at, callback)
                except RaiseException as exc:
                    message=exc.diag.message_primary
                    kind=next((k for k in ('text','image','voice','analysis') if f'({k})' in message),None)
                    reply=self._quota_card(c,kind) if kind and 'Квота AI исчерпана' in message else Reply(message)
                except ValueError as exc:
                    reply = Reply(str(exc))
                c.execute('INSERT INTO telegram_updates(bot_id,update_id,user_id,response,workspace_id) VALUES(%s,%s,%s,%s,coalesce(%s,current_workspace()))',
                          (bot_id, update_id, user_id, Jsonb(asdict(reply)),UUID(reply.access_workspace_id) if reply.access_workspace_id else None))
        if reply.text_job_id:return self._resolve_text_job(telegram_id,reply.text_job_id)
        if reply.capture_batch_id:return self._resolve_capture_batch(telegram_id,reply.capture_batch_id)
        if reply.share_id:return self._resolve_share(telegram_id,reply.share_id)
        if reply.attachment_id:return self._resolve_document(telegram_id,reply.attachment_id)
        if reply.deleted_attachment_id:return self._remove_document_file(telegram_id,reply.deleted_attachment_id)
        if reply.report_id:
            return self._resolve_report(telegram_id,reply.report_id,reply.report_format)
        if reply.report_job_id:
            return self._resolve_report_job(telegram_id,reply.report_job_id)
        if reply.sheets_connection_id:
            return self._resolve_sheets_connection(telegram_id,reply.sheets_connection_id)
        if reply.receipt_operation_id:
            return self._resolve_receipt_attachment(telegram_id,reply.receipt_operation_id)
        if reply.receipt_job_id:
            return self._resolve_receipt(telegram_id,reply.receipt_job_id)
        return self._resolve_job(telegram_id, reply.job_id) if reply.job_id else reply

    def _draft(self, c):
        return c.execute("SELECT * FROM operation_drafts WHERE state='pending' AND author_user_id=actor_user_id()").fetchone()

    def _prompt(self, c, d):
        captured=self._capture_draft(c,d)
        if captured is not None:return captured
        CURRENCY.set(self._account_currency(c,d['account_id']))
        if d['kind']!='expense' or d['edit_operation_id'] or d['finance_edit_field']:
            return self._finance_prompt(c,d)
        if d['voice_job_id']:
            return self._voice_prompt(c,d)
        if d['receipt_batch_id']:
            return self._receipt_prompt(c,d)
        suffix = '\n/cancel — отмена.'
        if d['step'] == 'amount':
            return Reply('Сколько потратили? Введите сумму в валюте выбранного счёта, например 850,50.' + suffix)
        if d['step'] == 'ai_pending':
            job=c.execute('SELECT id FROM ai_jobs WHERE draft_id=%s AND draft_version=%s',(d['id'],d['version'])).fetchone()
            return Reply('Определяю категорию…',job_id=str(job['id']) if job else None)
        if d['step'] == 'category':
            return self._category_menu(c,d)
        if d['step'] == 'category_review':
            category=c.execute('SELECT name FROM categories WHERE id=%s',(d['category_id'],)).fetchone()['name']
            source='Ваше личное правило' if d['category_source']=='rule' else 'AI предлагает'
            return Reply(f"{source}: {category}\nПокупка: {d['description']}\nПодтвердите или измените категорию.",
                         [[('Подтвердить категорию',f"accept:{d['id']}:{d['version']}")],
                          [('Другая категория',f"recat:{d['id']}:{d['version']}")]])
        if d['step'] == 'description':
            return Reply('На что потратили? Введите описание (до 500 символов) или «-», чтобы пропустить.' + suffix)
        if d['step'] == 'date':
            return Reply('Когда потратили? «Сегодня», «вчера» или ДД.ММ.ГГГГ.\n'
                         + f"«Сегодня» — дата начала этого расхода: {d['source_sent_at'].astimezone(ZoneInfo(d['timezone_snapshot'])):%d.%m.%Y}." + suffix)
        category = c.execute('SELECT name FROM categories WHERE id=%s', (d['category_id'],)).fetchone()['name']
        return Reply(f"Расход\nСумма: {money(d['amount'])}\n"
                     f"Категория: {category}\nОписание: {d['description'] or '—'}\nДата: {d['occurred_on']:%d.%m.%Y}\n"
                     ""+('\nВозможный дубль: уже есть расход с такой суммой и датой. Подтверждайте только отдельную покупку.' if self._receipt_duplicates(c,d) else ''),
                     [[('Сохранить', f"save:{d['id']}:{d['version']}")], [('Изменить', f"edit:{d['id']}:{d['version']}"), ('Отмена', f"cancel:{d['id']}:{d['version']}")], [('Категория',f"recat:{d['id']}:{d['version']}")]])

    def _new(self, c, user_id, sent_at, amount=None, flow='auto'):
        if self._document_upload(c):return Reply('Сначала завершите прикрепление документа или /cancel.')
        if self._media_queue(c):return self._media_blocker(c)
        pending = self._draft(c)
        if pending:
            reply = self._prompt(c, pending)
            reply.text = 'У вас есть незавершённый расход. Продолжите его или отправьте /cancel.\n\n' + reply.text
            return reply
        if self._voice_busy(c):
            return Reply('Голос ещё обрабатывается. /voice — состояние; /cancel — отмена.')
        batch=c.execute("SELECT * FROM receipt_batches WHERE author_user_id=%s AND state IN ('collecting','processing')",(user_id,)).fetchone()
        if batch:
            return Reply('Сначала завершите или отмените чек: /receipts, /cancel.')
        row = self._account_context(c)
        c.execute('INSERT INTO operation_drafts(workspace_id,author_user_id,account_id,timezone_snapshot,source_sent_at,amount,step,flow) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (row['id'], user_id, row['account_id'], row['timezone'], sent_at, amount, ('category' if flow=='manual' else 'description') if amount is not None else 'amount', flow))
        c.execute('UPDATE operation_drafts SET automatic_capture=%s WHERE id=%s',(flow=='auto' and self._capture_enabled(c),self._draft(c)['id']))
        return self._prompt(c, self._draft(c))

    def _dispatch(self, c, user_id, text, sent_at, callback):
        simple=self._simple_entry(c,user_id,text,sent_at,callback)
        if simple is not None:return simple
        ui=self._ui_entry(c,user_id,text,sent_at,callback)
        if ui is not None:return ui
        if callback:
            if callback.startswith('incoming:'):
                _,kind,identity,version=callback.split(':')
                if kind not in ('income','opening'):return Reply('Тип недоступен.')
                d=c.execute("UPDATE operation_drafts SET kind=%s,capture_kind_pending=false,version=version+1 WHERE id=%s AND version=%s AND state='pending' AND capture_kind_pending RETURNING *",(kind,UUID(identity),int(version))).fetchone()
                return self._prompt(c,d) if d else Reply('Запись уже обработана или недоступна.')
            if callback in ('captureon','captureoff'):
                c.execute('UPDATE user_settings SET automatic_capture=%s WHERE user_id=%s',(callback=='captureon',user_id))
                return Reply('Автосохранение '+('включено.' if callback=='captureon' else 'выключено. Записи сохраняются после подтверждения.'),[[('Настройки','ui:go:settings')]])
            currency_choice=self._user_currency_callback(c,callback)
            if currency_choice is not None:return currency_choice
            addon=self._addon_callback(c,user_id,callback)
            if addon is not None:return addon
            privacy=self._privacy_callback(c,user_id,callback)
            if privacy is not None:return privacy
            admin=self._admin_callback(c,user_id,callback)
            if admin is not None:return admin
            billing=self._billing_callback(c,user_id,callback,sent_at)
            if billing is not None:return billing
            notice=self._notification_callback(c,user_id,callback,sent_at)
            if notice is not None:return notice
            sharing_reply=self._sharing_callback(c,user_id,callback)
            if sharing_reply is not None:return sharing_reply
            media_reply=self._media_callback(c,user_id,callback,sent_at)
            if media_reply is not None:return media_reply
            documents_reply=self._documents_callback(c,user_id,callback,sent_at)
            if documents_reply is not None:return documents_reply
            funds_reply=self._funds_callback(c,user_id,callback,sent_at)
            if funds_reply is not None:return funds_reply
            workspace_reply=self._workspace_callback(c,user_id,callback,sent_at)
            if workspace_reply is not None:
                return workspace_reply
            input_reply=self._input_callback(c,user_id,callback,sent_at)
            if input_reply is not None:
                return input_reply
            finance_reply=self._finance_callback(c,user_id,callback,sent_at)
            if finance_reply is not None:
                return finance_reply
            report_reply=self._report_callback(c,user_id,callback)
            if report_reply is not None:
                return report_reply
            voice_reply=self._voice_callback(c,user_id,callback)
            if voice_reply is not None:
                return voice_reply
            receipt_reply=self._receipt_callback(c,user_id,callback)
            if receipt_reply is not None:
                return receipt_reply
            category_reply=self._category_callback(c,user_id,callback)
            if category_reply is not None:
                return category_reply
            if callback == 'howto':
                return Reply(QUICK_HELP, [[('Моя подписка','subscription'),('Ежемесячный отчёт','monthlysettings')],[('☰ Все действия','ui:menu')],[('Назад','start')]])
            if callback in ('add', 'history', 'report', 'accounts', 'start', 'subscription', 'renewal', 'workspaces'):
                return self._dispatch(c, user_id, '/' + callback, sent_at, None)
            action, _, raw_id = callback.partition(':')
            if action not in ('save', 'edit', 'cancel'):
                return Reply('Эта кнопка больше не поддерживается. /help')
            try:
                raw_id,_,raw_version=raw_id.partition(':')
                draft_id = UUID(raw_id)
                expected_version=int(raw_version or '1')
            except ValueError:
                return Reply('Недействительная кнопка. /add — новый расход.')
            d = c.execute('SELECT * FROM operation_drafts WHERE id=%s AND author_user_id=%s FOR UPDATE', (draft_id, user_id)).fetchone()
            if d:CURRENCY.set(self._account_currency(c,d['account_id']))
            if not d or d['state'] == 'cancelled':
                return Reply('Черновик недоступен, отменён или истёк. /add — новый расход.')
            if d['version']!=expected_version:
                return Reply('Эта карточка устарела. /add — показать текущий расход.')
            if d['state'] == 'saved':
                return Reply('Этот расход уже сохранён. Повторной записи нет.', MENU)
            if action == 'cancel':
                c.execute("UPDATE operation_drafts SET state='cancelled' WHERE id=%s", (draft_id,))
                return Reply('Черновик отменён. Сохранённые расходы не изменились.', MENU)
            if action == 'edit':
                c.execute("UPDATE operation_drafts SET state='cancelled' WHERE id=%s", (draft_id,))
                return self._new(c, user_id, sent_at, flow=d['flow'])
            if d['voice_job_id']:
                if d['voice_edit_field'] or not all((d['amount'],d['description'],d['category_id'],d['occurred_on'])) or (self._receipt_duplicates(c,d) and not d['duplicate_confirmed']):
                    return self._voice_prompt(c,d)
                c.execute("UPDATE operation_drafts SET step='confirm' WHERE id=%s",(d['id'],))
                d['step']='confirm'
            if d['step'] != 'confirm':
                return Reply('Сначала завершите ввод расхода.')
            if d['receipt_batch_id']:
                if (not d['payment_confirmed'] or d['receipt_currency']!=self._account_currency(c,d['account_id']) or d['receipt_edit_field']
                    or (self._receipt_duplicates(c,d) and not d['duplicate_confirmed'])):
                    return self._receipt_prompt(c,d)
            operation=self._save_operation(c,draft_id)
            return Reply(f"Расход {money(d['amount'])} сохранён.\n/history — история; /add — следующий расход.", [[('Исправить категорию',f'opcat:{operation}')],[('Добавить чек/документ',f'docadd:operation:{operation}')]] + ([[('Чек',f'rview:{operation}'),('Позиции',f'ritems:{operation}')]] if d['receipt_batch_id'] else []) + MENU)
        parts = text.split(maxsplit=1)
        command = parts[0].split('@')[0].lower() if parts else ''
        arg = parts[1] if len(parts) > 1 else ''
        if command=='/cancel' or (command=='/ai' and arg=='off'):
            c.execute("UPDATE text_jobs SET state='cancelled',reply=%s WHERE state IN ('queued','running')",(Jsonb(asdict(Reply('Распознавание текста отменено.'))),))
        currency=self._currency_command(c,user_id,command,arg,sent_at)
        if currency is not None:return currency
        privacy=self._privacy_command(c,user_id,command,arg,sent_at)
        if privacy is not None:return privacy
        admin=self._admin_command(c,user_id,command,arg,sent_at)
        if admin is not None:return admin
        billing=self._billing_command(c,user_id,command,arg,sent_at)
        if billing is not None:return billing
        planning=self._planning_command(c,user_id,command,arg,sent_at)
        if planning is not None:return planning
        media_reply=self._media_command(c,user_id,command,arg,sent_at)
        if media_reply is not None:return media_reply
        documents_reply=self._documents_command(c,user_id,command,arg,sent_at)
        if documents_reply is not None:return documents_reply
        funds_reply=self._funds_command(c,user_id,command,arg,sent_at)
        if funds_reply is not None:return funds_reply
        workspace_reply=self._workspace_command(c,user_id,command,arg,sent_at)
        if workspace_reply is not None:
            return workspace_reply
        input_reply=self._input_command(c,user_id,command,arg,sent_at)
        if input_reply is not None:
            return input_reply
        finance_reply=self._finance_command(c,user_id,command,arg,sent_at)
        if finance_reply is not None:
            return finance_reply
        report_reply=self._report_command(c,user_id,command,arg,sent_at)
        if report_reply is not None:
            return report_reply
        voice_reply=self._voice_command(c,user_id,command,arg)
        if voice_reply is not None:
            return voice_reply
        receipt_reply=self._receipt_command(c,user_id,command,arg)
        if receipt_reply is not None:
            return receipt_reply
        category_reply=self._category_command(c,user_id,command,arg)
        if category_reply is not None:
            return category_reply
        if command == '/start':
            return self._welcome(c)
        if command == '/files':return self._storage_overview(c)
        if command == '/help':
            intro=c.execute("SELECT value FROM service_content WHERE key='help_intro'").fetchone()
            reply=self._ui_menu(c)
            if intro:reply.text=intro['value']
            return reply
        if command == '/support':
            return Reply(self.support or 'Контакт поддержки пока не настроен владельцем бота.')
        if command == '/cancel':
            d = self._draft(c)
            if d:
                c.execute("UPDATE operation_drafts SET state='cancelled' WHERE id=%s", (d['id'],))
            return Reply('Черновик отменён.' if d else 'Нет активного черновика.', MENU)
        if command in ('/add','/manual'):
            return self._new(c, user_id, sent_at, amount_from_text(arg) if arg else None,flow='manual' if command=='/manual' else 'auto')
        if command == '/settings':
            if arg:
                try:
                    ZoneInfo(arg)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError('Неизвестный часовой пояс. Пример: /settings Europe/Moscow') from None
                c.execute('UPDATE user_settings SET timezone=%s WHERE user_id=%s', (arg, user_id))
            zone = c.execute('SELECT timezone FROM user_settings WHERE user_id=%s', (user_id,)).fetchone()['timezone']
            return self._simple_settings(c)
        if command == '/history':
            if arg and (not arg.isascii() or not arg.isdigit() or len(arg)>6 or int(arg)<1):
                raise ValueError('Номер страницы должен быть положительным целым: /history 2')
            page = int(arg or '1')
            return self._history_page(c,page)
        if command.startswith('/'):
            return Reply('Неизвестная команда. /help — доступные команды.')
        media=self._media_queue(c)
        if media:return self._media_text(c,media,text)
        d = self._draft(c)
        if not d:
            if self._capture_enabled(c):return self._free_text(c,user_id,text,sent_at)
            try:
                amount = amount_from_text(text)
            except ValueError:
                return self._free_text(c,user_id,text,sent_at)
            return self._new(c, user_id, sent_at, amount)
        if d['capture_kind_pending']:return self._prompt(c,d)
        if d['voice_job_id'] and not d['edit_operation_id']:
            voice_reply=self._voice_text(c,d,text)
            if voice_reply is not None:return voice_reply
        if d['kind']!='expense' or d['edit_operation_id'] or d['finance_edit_field']:
            return self._finance_text(c,d,text)
        if d['voice_job_id']:
            voice_reply=self._voice_text(c,d,text)
            if voice_reply is not None:
                return voice_reply
        receipt_reply=self._receipt_text(c,d,text)
        if receipt_reply is not None:
            return receipt_reply
        if d['step'] == 'amount':
            c.execute("UPDATE operation_drafts SET amount=%s,step=%s WHERE id=%s", (amount_from_text(text), 'category' if d['flow']=='manual' else 'description', d['id']))
        elif d['step'] == 'category':
            category = c.execute('SELECT id FROM categories WHERE NOT archived AND workspace_id=%s AND lower(name)=lower(%s)', (d['workspace_id'], text)).fetchone()
            if not category:
                reply = self._prompt(c, d)
                reply.text = 'Категория не найдена.\n' + reply.text
                return reply
            return self._select_category(c,d,category['id'])
        elif d['step'] == 'description':
            if not text or len(text)>500:
                raise ValueError('Описание: от 1 до 500 символов. «-» — пропустить.')
            c.execute("UPDATE operation_drafts SET description=%s,step='date' WHERE id=%s", ('' if text=='-' else text, d['id']))
            if d['flow']=='auto' and d['category_id'] is None:
                return self._suggest(c,self._draft(c))
        elif d['step'] == 'date':
            # Relative dates are based on the original input, even after a restart/midnight.
            day = date_from_text(text, d['source_sent_at'], d['timezone_snapshot'])
            c.execute("UPDATE operation_drafts SET occurred_on=%s,step='confirm' WHERE id=%s", (day, d['id']))
        return self._prompt(c, self._draft(c))
