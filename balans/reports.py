"""User-scoped immutable report snapshots and durable external jobs."""
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal,ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo
import base64
import logging
import secrets
from psycopg.types.json import Jsonb
from balans.domain import Reply
from balans.report_data import period,summarize,fmt,amount,csv_bytes,MAX_ROWS
from balans.report_pdf import render_pdf
from balans.sheets import spreadsheet_id

log=logging.getLogger('balans.reports')

def report_uuid(value):
    try:
        return UUID(value)
    except (ValueError,TypeError,AttributeError):
        raise ValueError('Недействительная кнопка отчёта. /report — создать новый.') from None



class Reports:
    def _report_card(self,report):
        s=report['snapshot'];summary=s['summary'];identity=report['id']
        from balans.category_settings import label
        from datetime import date
        currency=s.get('currency','RUB');symbol='₽' if currency=='RUB' else currency
        def money(value):return fmt(value)+' '+symbol
        start=date.fromisoformat(s['start']);end=date.fromisoformat(s['end'])
        lines=[f"📊 {start:%d.%m.%Y} — {end:%d.%m.%Y}",
               '',f"➕ Доходы — {money(summary.get('income','0'))}",f"➖ Расходы — {money(summary['total'])}"]
        refunds=amount(summary.get('refunds','0'))
        if refunds:lines.append('↩️ Возвраты — '+money(refunds))
        flow=amount(summary.get('income','0'))-amount(summary['total'])+refunds
        lines.append(('💚 Разница за период: +' if flow>=0 else '🔻 Разница за период: −')+money(abs(flow)))
        if summary['categories']:
            lines+=['','На что потратили']+[label(x['name'])+' — '+money(x['total']) for x in summary['categories'][:5]]
            if len(summary['categories'])>5:lines.append('Остальные категории — в PDF-отчёте.')
        elif not summary.get('operation_count'):lines+=['','За этот период записей пока нет.']
        if s.get('delta_percent') is not None and amount(s['previous']['total'])>0:
            percent=amount(s['delta_percent'])
            prev_start=date.fromisoformat(s['previous_start']);prev_end=date.fromisoformat(s['previous_end'])
            lines+=['',f"Расходы на {abs(percent)}% {'больше' if percent>=0 else 'меньше'}, чем за {prev_start:%d.%m.%Y} — {prev_end:%d.%m.%Y}."]
        return Reply('\n'.join(lines),[
            [('📄 Скачать PDF-отчёт',f'rpdf:{identity}')],
            [('🧾 Детализированный отчёт',f'rdetail:{identity}')],
            [('🤖 Анализ расходов',f'rask:{identity}')],
            [('📅 Изменить период','ui:go:report_period')],
            [('☰ Меню','ui:menu')]],command_hints=False)

    def _get_report(self,c,identity):
        return c.execute('SELECT * FROM reports WHERE id=%s AND expires_at>now()',(report_uuid(str(identity)),)).fetchone()

    def _analysis_consent(self,report):
        if not report['snapshot']['summary']['count']:return Reply('Нет операций: AI-анализ не вызывается.')
        if not self.report_ai.available:return Reply('AI-анализ временно недоступен. PDF и CSV работают без него.')
        return Reply('Для анализа в OpenAI будут отправлены суммы и количества по категориям и дням за выбранный и сравниваемый периоды. '
                     'Описания покупок, чеки, голос и Telegram ID не передаются. Выводы основаны только на записях бота.\n'
                     f"Период: {report['snapshot']['start']} — {report['snapshot']['end']}.",[[('Разрешить и проанализировать',f"ranalyze:{report['id']}")]])

    def _sheets_consent(self,c,report):
        if not self.sheets.available:return Reply('Прямой экспорт Google Sheets ещё не настроен владельцем бота. Пока можно получить CSV и импортировать его в Google Sheets.')
        connection=c.execute("SELECT * FROM sheets_connections WHERE state='active'").fetchone()
        if not connection:return Reply('Сначала подключите свою таблицу: /sheets connect ССЫЛКА. /sheets — инструкция.')
        snapshot=report['snapshot']
        return Reply(f"Экспорт {snapshot['start']} — {snapshot['end']}: {snapshot['summary'].get('operation_count',snapshot['summary']['count'])} операций, {fmt(snapshot['summary']['total'])} ₽.\n"
                     'В таблицу будут переданы ID, даты, суммы, категории, счета, описания, продавцы, источники, бюджет, Telegram ID участников, статусы проверки и получения, номера версий и число документов. Все участники с доступом к этой таблице смогут их видеть. '
                     'Будет создан отдельный лист; существующие листы не изменятся.\n'
                     f"Таблица: https://docs.google.com/spreadsheets/d/{connection['spreadsheet_id']}/edit",
                     [[('Экспортировать в эту таблицу',f"rexport:{report['id']}:{connection['id'].hex[:16]}")]])

    def _report_command(self,c,user_id,command,arg,sent_at):
        if command=='/sheets':
            return self._sheets_command(c,user_id,arg)
        if command not in ('/report','/pdf','/analyze','/csv'):return None
        report=self._report_snapshot(c,user_id,arg,sent_at)
        if command=='/pdf':return Reply('Готовлю PDF…',report_id=str(report['id']),report_format='pdf')
        if command=='/csv':return Reply('Готовлю CSV…',report_id=str(report['id']),report_format='csv')
        if command=='/analyze':return self._analysis_consent(report)
        return self._report_card(report)

    def _sheets_command(self,c,user_id,arg):
        return Reply('Подключение таблиц отключено. Отчёт можно скачать файлом.',[[('Отчёт','report')]])

    def _legacy_sheets_command(self,c,user_id,arg):
        if arg=='off':
            c.execute("UPDATE sheets_connections SET state='revoked' WHERE state IN ('active','pending')")
            return Reply('Подключение Google Sheets отключено. Уже экспортированные копии остаются в Google.')
        if not self.sheets.available:
            return Reply('Google Sheets пока не настроен: владельцу бота нужно указать GOOGLE_SERVICE_ACCOUNT_FILE. До подключения доступен /csv — файл для импорта в Google Sheets.')
        if arg.startswith('connect '):
            identity=spreadsheet_id(arg[8:].strip());challenge='balans-'+secrets.token_urlsafe(24)
            c.execute("UPDATE sheets_connections SET state='revoked' WHERE state IN ('active','pending')")
            connection=c.execute('INSERT INTO sheets_connections(author_user_id,spreadsheet_id,challenge) VALUES(%s,%s,%s) RETURNING id',(user_id,identity,challenge)).fetchone()
            return Reply('Предоставьте доступ редактора сервисному аккаунту '+self.sheets.email+'.\n'
                         'В выбранной таблице создайте пустой лист «Баланс-доступ» и вставьте в A1 этот код обычным текстом:\n'+challenge+
                         '\nКод действует 30 минут для подключения. Он подтверждает ваше право редактировать таблицу. Оставьте лист с кодом для дальнейшего экспорта.',
                         [[('Проверить подключение',f"sverify:{connection['id']}")]])
        return Reply('Создайте Google-таблицу и дайте доступ редактора '+self.sheets.email+'. Затем /sheets connect ССЫЛКА. '
                     'Бот попросит подтвердить доступ кодом в специальном листе. Экспорт: /report → Google Sheets → подтверждение. Отключить: /sheets off.')

    def _report_callback(self,c,user_id,callback):
        action,_,raw=callback.partition(':')
        if action=='sverify':
            identity=report_uuid(raw)
            connection=c.execute("SELECT id FROM sheets_connections WHERE id=%s AND state IN ('pending','active')",(identity,)).fetchone()
            return Reply('Проверяю подключение…',sheets_connection_id=str(identity)) if connection else Reply('Подключение недоступно.')
        if action=='jretry':
            job=c.execute('SELECT * FROM report_jobs WHERE id=%s FOR UPDATE',(report_uuid(raw),)).fetchone()
            if not job:return Reply('Задание недоступно.')
            if job['kind']=='sheets':return Reply('Подключение таблиц отключено.',[[('Отчёт','report')]])
            if job['state']=='failed':c.execute("UPDATE report_jobs SET state='pending',reply=NULL,error_code=NULL WHERE id=%s",(job['id'],))
            return Reply('Проверяю задание…',report_job_id=str(job['id']))
        if action not in ('rdetail','rpdf','rcsv','raudit','rask','ranalyze','rsask','rexport'):return None
        identity,_,connection_token=raw.partition(':')
        report=self._get_report(c,identity)
        if not report:return Reply('Отчёт недоступен или истёк. /report — создать новый.')
        if action in ('rdetail','rpdf','rcsv','raudit'):return Reply('Готовлю файл…',report_id=str(report['id']),report_format={'rdetail':'detailed','rpdf':'pdf','rcsv':'csv','raudit':'auditcsv'}[action])
        if action=='rask':return self._analysis_consent(report)
        if action=='rsask':return self._sheets_consent(c,report)
        kind='analysis' if action=='ranalyze' else 'sheets';connection=None
        if kind=='analysis':
            if not report['snapshot']['summary']['count'] or not self.report_ai.available:return self._analysis_consent(report)
        else:
            connection=c.execute("SELECT * FROM sheets_connections WHERE state='active'").fetchone()
            if not connection or connection['id'].hex[:16]!=connection_token:return Reply('Подключение изменилось. Нажмите Google Sheets в отчёте ещё раз.')
        job=c.execute('INSERT INTO report_jobs(workspace_id,author_user_id,report_id,kind,connection_id,tab_id,model,prompt_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(report_id,kind) DO NOTHING RETURNING id',
                      (report['workspace_id'],user_id,report['id'],kind,connection['id'] if connection else None,secrets.randbelow(2_000_000_000)+1 if connection else None,self.report_ai.model if kind=='analysis' else None,'report-v1' if kind=='analysis' else None)).fetchone()
        if not job:job=c.execute('SELECT id FROM report_jobs WHERE report_id=%s AND kind=%s',(report['id'],kind)).fetchone()
        return Reply('Готовлю анализ…' if kind=='analysis' else 'Экспортирую…',report_job_id=str(job['id']))

    def _resolve_report(self,actor,identity,format):
        with self._actor_transaction(actor) as c:
            report=self._get_report(c,identity)
            if not report:return Reply('Отчёт недоступен или истёк. /report — создать новый.')
            if format=='detailed':
                from balans.detailed_report import collect_sources
                try:sources,files=collect_sources(self,c,report)
                except ValueError as exc:return Reply(str(exc),[[('📅 Изменить период','ui:go:report_period')]])
        snapshot=report['snapshot']
        if format=='detailed':
            from balans.detailed_report import render_detailed
            try:data=render_detailed(snapshot,sources,files)
            except ValueError as exc:return Reply(str(exc),[[('📅 Изменить период','ui:go:report_period')]])
            return Reply('',generated_document=base64.b64encode(data).decode(),generated_filename=f"balans-detailed-{snapshot['start']}-{snapshot['end']}.pdf")
        data=render_pdf(snapshot) if format=='pdf' else csv_bytes(snapshot)
        return Reply('',generated_document=base64.b64encode(data).decode(),generated_filename=f"balans-{snapshot['start']}-{snapshot['end']}.{format}")

    def _resolve_sheets_connection(self,actor,identity):
        return Reply('Подключение таблиц отключено.',[[('Отчёт','report')]])

    def _legacy_resolve_sheets_connection(self,actor,identity):
        with self._actor_transaction(actor) as c:
            row=c.execute("SELECT * FROM sheets_connections WHERE id=%s AND (state='active' OR (state='pending' AND expires_at>now()))",(report_uuid(identity),)).fetchone()
            if not row:return Reply('Подключение отменено или код истёк. /sheets connect ССЫЛКА — начать заново.')
        try:
            verified=self.sheets.available and self.sheets.verify(row['spreadsheet_id'],row['challenge'])
        except Exception:
            verified=False
        with self._actor_transaction(actor) as c:
            if not verified:return Reply('Код или доступ редактора не подтверждён. Проверьте лист «Баланс-доступ», ячейку A1 и доступ сервисного аккаунта; затем нажмите проверку снова.')
            updated=c.execute("UPDATE sheets_connections SET state='active' WHERE id=%s AND (state='active' OR (state='pending' AND expires_at>now())) RETURNING id",(row['id'],)).fetchone()
            return Reply('Google Sheets подключены. /report → Google Sheets — экспорт выбранного периода.') if updated else Reply('Подключение отменено или истекло.')

    def _resolve_report_job(self,actor,identity):
        with self._actor_transaction(actor) as c:
            job=c.execute('SELECT *,lease_until>now() AS live FROM report_jobs WHERE id=%s FOR UPDATE',(report_uuid(identity),)).fetchone()
            if not job:return Reply('Задание недоступно.')
            if job['kind']=='sheets':return Reply('Подключение таблиц отключено.',[[('Отчёт','report')]])
            report=self._get_report(c,job['report_id'])
            if not report:return Reply('Отчёт истёк. /report — создать новый.')
            if job['reply']:return Reply(**job['reply'])
            if job['state']=='processing':
                if job['live']:return Reply('Задание выполняется. Нажмите ту же кнопку позже.')
                return self._report_failure(c,job,'interrupted')
            connection=None
            if job['kind']=='sheets':
                connection=c.execute("SELECT * FROM sheets_connections WHERE id=%s AND state='active'",(job['connection_id'],)).fetchone()
                if not connection:return self._report_failure(c,job,'connection_revoked')
            c.execute("UPDATE report_jobs SET state='processing',lease_until=now()+interval '3 minutes' WHERE id=%s",(job['id'],))
        try:
            if job['kind']=='analysis':
                result=self.report_ai.analyze(report['snapshot'])
                text='AI-анализ записанных расходов\n\n'+'\n'.join('• '+x for x in result.observations)+'\n\nРекомендации\n'+'\n'.join('• '+x for x in result.recommendations)+'\n\n'+result.limitation
                reply=Reply(text)
            else:
                url=self.sheets.export(connection['spreadsheet_id'],connection['challenge'],str(job['id']),job['tab_id'],report['snapshot'])
                reply=Reply('Экспорт готов. Создан отдельный лист со снимком расходов:\n'+url)
        except Exception as exc:
            log.warning('Ошибка отчёта (%s)',type(exc).__name__)
            with self._actor_transaction(actor) as c:return self._report_failure(c,job,type(exc).__name__)
        with self._actor_transaction(actor) as c:
            c.execute("UPDATE report_jobs SET state='ready',reply=%s,error_code=NULL WHERE id=%s",(Jsonb(asdict(reply)),job['id']))
        return reply

    def _report_failure(self,c,job,code):
        reply=Reply('Не удалось завершить '+('AI-анализ' if job['kind']=='analysis' else 'экспорт')+'. Данные учёта не изменились. '
                    +('Повтор вызывает новый AI-запрос.' if job['kind']=='analysis' else 'При повторе бот проверит, создан ли лист, чтобы не дублировать экспорт.'),
                    [[('Повторить',f"jretry:{job['id']}")]])
        c.execute("UPDATE report_jobs SET state='failed',error_code=%s,reply=%s WHERE id=%s",(code,Jsonb(asdict(reply)),job['id']))
        return reply
