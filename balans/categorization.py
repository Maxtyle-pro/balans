"""Category conversations, private learning and durable external API jobs."""
from contextlib import contextmanager
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from balans.ai import PROMPT_VERSION, Suggestion, match_rule, normalize
from balans.domain import Reply


class Categorization:
    @contextmanager
    def _actor_transaction(self, telegram_id):
        with self.pool.connection() as c, c.transaction():
            c.execute("SELECT set_config('search_path','balans,pg_catalog',true)")
            c.execute("SELECT set_config('balans.telegram_user_id',%s,true)", (str(telegram_id),))
            c.execute('SELECT pg_advisory_xact_lock(%s)', (telegram_id,))
            yield c

    def _categories(self, c, workspace_id):
        return c.execute('SELECT id,name FROM categories WHERE workspace_id=%s AND NOT archived ORDER BY name,id', (workspace_id,)).fetchall()

    def _category_menu(self, c, d):
        categories = self._categories(c, d['workspace_id'])
        buttons = [[(cat['name'], f"pick:{d['id']}:{i}:{d['version']}")] for i, cat in enumerate(categories)]
        return Reply('Выберите категорию покупки или отправьте её название:\n' + '\n'.join(cat['name'] for cat in categories), buttons)

    def _rule(self, c, d):
        rules = c.execute('SELECT * FROM category_rules WHERE workspace_id=%s AND author_user_id=actor_user_id() AND enabled', (d['workspace_id'],)).fetchall()
        return match_rule(d['description'] or '', rules)

    def _suggest(self, c, d):
        category = self._rule(c, d)
        if category:
            c.execute("UPDATE operation_drafts SET category_id=%s,category_source='rule',step='category_review' WHERE id=%s", (UUID(category), d['id']))
            return self._prompt(c, self._draft(c))
        consent = c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()['ai_enabled']
        if not self.ai.available or not consent or not normalize(d['description'] or ''):
            c.execute("UPDATE operation_drafts SET step='category',category_source='fallback' WHERE id=%s", (d['id'],))
            if d['automatic_capture']:return self._prompt(c,self._draft(c))
            reply = self._category_menu(c, self._draft(c))
            prefix = 'AI сейчас недоступен; выберите категорию вручную.' if consent else 'AI выключен. /ai — включить определение категории по описанию.'
            reply.text = prefix + '\n\n' + reply.text
            return reply
        if d['automatic_capture'] and (quota:=self._quota_preflight(c,'text')):
            c.execute("UPDATE operation_drafts SET step='category',category_source='fallback' WHERE id=%s",(d['id'],))
            reply=self._prompt(c,self._draft(c));reply.additional_replies.append(asdict(quota));return reply
        request = {'description':d['description'], 'categories':[{'id':str(cat['id']), 'name':cat['name']} for cat in self._categories(c, d['workspace_id'])]}
        job = c.execute('INSERT INTO ai_jobs(workspace_id,author_user_id,draft_id,draft_version,model,prompt_version,request) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                        (d['workspace_id'], d['author_user_id'], d['id'], d['version'], self.ai.model, PROMPT_VERSION, Jsonb(request))).fetchone()['id']
        c.execute("UPDATE operation_drafts SET step='ai_pending' WHERE id=%s", (d['id'],))
        return Reply('Определяю категорию…', job_id=str(job))

    def _resolve_job(self, telegram_id, raw_id):
        # Persist the job before calling the provider. No DB transaction during HTTP.
        job_id = UUID(raw_id)
        with self._actor_transaction(telegram_id) as c:
            job = c.execute('SELECT *, lease_until>now() AS leased FROM ai_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if not job:
                return Reply('Задача недоступна. /add — новый расход.')
            if job['reply']:
                return Reply(**job['reply'])
            d = c.execute("SELECT * FROM operation_drafts WHERE id=%s AND state='pending' AND expires_at>now()", (job['draft_id'],)).fetchone()
            consent = c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
            valid = d and d['version']==job['draft_version'] and d['step']=='ai_pending' and consent and consent['ai_enabled']
            if not valid:
                return self._finish_job(c, job, Suggestion(error_code='cancelled'), apply=False)
            if job['state']=='running' and job['leased']:
                return Reply('Категория ещё определяется. Можно выбрать её командой /category Название или отменить /cancel.')
            if job['state']=='running':
                # A crash/unknown provider outcome must not silently cause another paid call.
                return self._finish_job(c, job, Suggestion(error_code='interrupted'))
            c.execute("UPDATE ai_jobs SET state='running',started_at=now(),lease_until=now()+interval '3 minutes' WHERE id=%s", (job_id,))
            request = job['request']
        result = self.ai.classify(request)
        with self._actor_transaction(telegram_id) as c:
            job = c.execute('SELECT * FROM ai_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
            if not job:
                return Reply('Задача недоступна.')
            if job['reply']:
                return Reply(**job['reply'])
            return self._finish_job(c, job, result)

    def _finish_job(self, c, job, result, apply=True):
        d = c.execute("SELECT * FROM operation_drafts WHERE id=%s AND state='pending' AND expires_at>now()", (job['draft_id'],)).fetchone()
        consent = c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
        valid = apply and d and d['version']==job['draft_version'] and d['step']=='ai_pending' and consent and consent['ai_enabled']
        if not valid:
            reply = Reply('Автоподбор отменён: черновик или настройки изменились. /add — продолжить текущий ввод.')
            state = 'cancelled'
        else:
            # A newly created private rule also wins over an in-flight AI response.
            rule = self._rule(c, d)
            category = rule or result.category_id
            allowed = {str(cat['id']) for cat in self._categories(c,d['workspace_id'])}
            if category and category not in allowed:
                category = None
                result = Suggestion(error_code='invalid_category')
            c.execute('UPDATE operation_drafts SET category_id=%s,category_source=%s,category_confidence=%s,step=%s WHERE id=%s',
                      (UUID(category) if category else None, 'rule' if rule else ('ai' if category else 'fallback'), result.confidence,
                       'category_review' if category else 'category', d['id']))
            reply = self._prompt(c, self._draft(c))
            state = 'succeeded' if not result.error_code else 'failed'
            if not category and not d['automatic_capture']:
                reply.text = 'Не удалось уверенно определить категорию. Выберите её вручную.\n\n' + reply.text
        c.execute('UPDATE ai_jobs SET state=%s,category_id=%s,confidence=%s,error_code=%s,response_id=%s,input_tokens=%s,output_tokens=%s,finished_at=now(),reply=%s WHERE id=%s',
                  (state, UUID(result.category_id) if result.category_id and valid else None, result.confidence,
                   result.error_code, result.response_id,result.input_tokens,result.output_tokens,Jsonb(asdict(reply)),job['id']))
        return reply

    def _learning_offer(self, c, feedback_id, reply):
        if not feedback_id:
            return reply
        f = c.execute('SELECT f.*,cat.name FROM category_feedback f JOIN categories cat ON cat.id=f.new_category_id WHERE f.id=%s', (feedback_id,)).fetchone()
        if f and normalize(f['description']):
            reply.text += f"\n\nЗапомнить для повторной покупки «{f['description']}» → {f['name']}? Правило будет только вашим."
            reply.buttons.append([('Запомнить для меня',f"learn:{f['id']}")])
        return reply

    def _select_category(self, c, d, category_id, message_id=None):
        old = d['category_id']
        if d['description'] is None:
            step='description'
        else:
            step='confirm' if d['occurred_on'] is not None else 'date'
        version=d['version']+1
        c.execute("UPDATE operation_drafts SET category_id=%s,category_source='manual',step=%s,version=%s WHERE id=%s", (category_id,step,version,d['id']))
        if d.get('edit_operation_id') and not d.get('cancel_operation'):
            return self._finish_editor_change(c,self._draft(c),message_id)
        reply=self._prompt(c,self._draft(c))
        if d['description'] and old != category_id:
            feedback=c.execute('INSERT INTO category_feedback(workspace_id,author_user_id,draft_id,old_category_id,new_category_id,description,expected_version) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                               (d['workspace_id'],d['author_user_id'],d['id'],old,category_id,d['description'],version)).fetchone()['id']
            return self._learning_offer(c,feedback,reply)
        return reply

    def _category_callback(self, c, user_id, callback, message_id=None):
        if callback in ('ai_on','ai_off'):
            return self._category_command(c,user_id,'/ai','on' if callback=='ai_on' else 'off')
        action,_,args=callback.partition(':')
        if action in ('accept','recat','pick'):
            parts=args.split(':')
            try:
                draft_id=UUID(parts[0]); version=int(parts[-1])
            except (ValueError,IndexError):
                return Reply('Недействительная кнопка категории.')
            d=self._draft(c)
            if not d or d['id']!=draft_id or d['version']!=version:
                return Reply('Эта карточка устарела. /add — показать текущий расход.')
            if d.get('edit_operation_id'):self._remember_editor_message(c,d,message_id)
            if action=='accept':
                if d['step']!='category_review':
                    return Reply('Эта кнопка уже использована.')
                c.execute("UPDATE operation_drafts SET step=CASE WHEN occurred_on IS NULL THEN 'date' ELSE 'confirm' END,version=version+1 WHERE id=%s",(draft_id,))
                return self._prompt(c,self._draft(c))
            if action=='recat':
                c.execute("UPDATE operation_drafts SET step='category',version=version+1 WHERE id=%s",(draft_id,))
                return self._editor_reply(d,self._category_menu(c,self._draft(c)))
            if d['step']!='category' or len(parts)!=3:
                return Reply('Выбор категории уже завершён.')
            categories=self._categories(c,d['workspace_id'])
            try:
                index=int(parts[1])
                if not 0<=index<len(categories):
                    raise ValueError
            except ValueError:
                return Reply('Недействительная категория.')
            return self._select_category(c,d,categories[index]['id'],message_id)
        if action=='opcat':
            try:
                operation_id=UUID(args)
            except ValueError:
                return Reply('Недействительная операция.')
            o=c.execute("SELECT o.*,r.description,r.amount,cat.name FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id JOIN categories cat ON cat.id=r.category_id WHERE o.state='active' AND o.kind='expense' AND o.id=%s AND o.created_by_user_id=%s", (operation_id,user_id)).fetchone()
            if not o:
                return Reply('Операция недоступна.')
            buttons=[]
            for cat in self._categories(c,o['workspace_id']):
                token=c.execute('INSERT INTO category_actions(workspace_id,author_user_id,operation_id,expected_revision_id,category_id) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                                (o['workspace_id'],user_id,o['id'],o['current_revision_id'],cat['id'])).fetchone()['id']
                buttons.append([(cat['name'],f'choose:{token}')])
            return Reply(f"Исправление категории: {o['description'] or '—'}\nСейчас: {o['name']}\nВыберите новую категорию. Сумма не изменится.",buttons)
        if action=='choose':
            try:
                token=UUID(args)
            except ValueError:
                return Reply('Недействительная кнопка.')
            a=c.execute('SELECT a.*,o.current_revision_id,a.expires_at>now() AS fresh FROM category_actions a JOIN operations o ON o.id=a.operation_id WHERE a.id=%s', (token,)).fetchone()
            if not a or not a['fresh'] or a['consumed_at'] or a['expected_revision_id']!=a['current_revision_id']:
                return Reply('Выбор устарел или уже применён. Откройте категорию заново из /history.')
            f=self._workspace_call(c,'SELECT change_category(%s) AS id',(token,))['id']
            return self._learning_offer(c,f,Reply('Категория обновлена. Сумма и остаток не изменились.'))
        if action=='learn':
            try:
                feedback_id=UUID(args)
            except ValueError:
                return Reply('Недействительное правило.')
            f=c.execute("SELECT *,created_at>now()-interval '24 hours' AS fresh FROM category_feedback WHERE id=%s",(feedback_id,)).fetchone()
            if not f or not f['fresh']:
                return Reply('Предложение недоступно или истекло.')
            if f['learned_at']:
                return Reply('Это правило уже было сохранено. /rules — ваши правила.')
            if f['draft_id']:
                valid=c.execute("SELECT 1 FROM operation_drafts WHERE id=%s AND state IN ('pending','saved') AND version=%s AND category_id=%s AND description=%s AND (state='saved' OR expires_at>now())", (f['draft_id'],f['expected_version'],f['new_category_id'],f['description'])).fetchone()
                # A later correction to the saved operation invalidates draft feedback too.
                changed=c.execute('SELECT 1 FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id WHERE o.source_draft_id=%s AND r.category_id<>%s',(f['draft_id'],f['new_category_id'])).fetchone()
                valid=valid and not changed
            else:
                valid=c.execute('SELECT 1 FROM operations WHERE id=%s AND current_revision_id=%s',(f['operation_id'],f['expected_revision_id'])).fetchone()
            if not valid or not normalize(f['description']):
                return Reply('Категория уже изменилась или черновик отменён. Это правило не сохранено.')
            self._upsert_rule(c,f['workspace_id'],user_id,'description',normalize(f['description']),f['new_category_id'])
            c.execute('UPDATE category_feedback SET learned_at=now() WHERE id=%s',(feedback_id,))
            return Reply('Запомнил для повторных покупок с таким описанием. /rules — просмотр и отключение правил.')
        if action=='disable':
            try:
                rule_id=UUID(args)
            except ValueError:
                return Reply('Недействительное правило.')
            row=c.execute('UPDATE category_rules SET enabled=false,updated_at=now() WHERE id=%s AND author_user_id=%s RETURNING id',(rule_id,user_id)).fetchone()
            return Reply('Правило отключено.' if row else 'Правило недоступно.')
        return None

    def _upsert_rule(self,c,workspace_id,user_id,kind,pattern,category_id):
        member=c.execute('SELECT id FROM memberships WHERE workspace_id=%s AND user_id=%s',(workspace_id,user_id)).fetchone()['id']
        c.execute('INSERT INTO category_rules(workspace_id,membership_id,author_user_id,match_kind,pattern,category_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,author_user_id,match_kind,pattern) DO UPDATE SET category_id=excluded.category_id,enabled=true,updated_at=now()',
                  (workspace_id,member,user_id,kind,pattern,category_id))

    def _category_command(self,c,user_id,command,arg):
        if command=='/ai':
            if arg not in ('','on','off'):
                return Reply('Используйте /ai, /ai on или /ai off.')
            if arg:
                enabled=arg=='on'
                c.execute("UPDATE user_settings SET ai_enabled=%s,ai_consent_version=CASE WHEN %s THEN 'category-ai-v1' ELSE ai_consent_version END,ai_consented_at=CASE WHEN %s THEN now() ELSE ai_consented_at END WHERE user_id=%s",(enabled,enabled,enabled,user_id))
                if not enabled:
                    c.execute("UPDATE text_jobs SET state='cancelled',reply=%s WHERE state IN ('queued','running')",(Jsonb(asdict(Reply('Распознавание текста отменено.'))),))
                    c.execute("UPDATE operation_drafts SET step='category',category_source='fallback',version=version+1 WHERE state='pending' AND step='ai_pending'")
                return Reply('ИИ-распознавание текста включено.' if enabled else 'ИИ-распознавание текста выключено. Ручной ввод доступен.')
            enabled=c.execute('SELECT ai_enabled FROM user_settings WHERE user_id=%s',(user_id,)).fetchone()['ai_enabled']
            return Reply(('AI включён.' if enabled else 'AI выключен.')+'\nПри включении текст сообщения, дата сообщения, валюта учёта и список категорий отправляются в OpenAI для определения типа, суммы, даты и категории операции. Telegram ID и история операций не передаются. '
                         'Распознанную запись можно исправить кнопкой «Изменить». ИИ можно отключить кнопкой ниже.',[[('Выключить' if enabled else 'Включить AI','ai_off' if enabled else 'ai_on')]])
        if command=='/category':
            d=self._draft(c)
            if not d:
                return Reply('Нет черновика. Для сохранённого расхода нажмите «Категория» в /history.')
            if d['amount'] is None:
                return Reply('Сначала введите сумму расхода.')
            if not arg:
                c.execute("UPDATE operation_drafts SET step='category',version=version+1 WHERE id=%s",(d['id'],))
                return self._category_menu(c,self._draft(c))
            category=c.execute('SELECT id FROM categories WHERE NOT archived AND workspace_id=%s AND lower(name)=lower(%s)',(d['workspace_id'],arg)).fetchone()
            if not category:
                return Reply('Категория не найдена. /category — показать список.')
            return self._select_category(c,d,category['id'])
        if command=='/rule':
            pattern,separator,name=arg.partition('|')
            pattern=normalize(pattern)
            if not separator or not 1<=len(pattern)<=100:
                return Reply('Правило по словам: /rule такси | Транспорт\nСовпадение по целым словам, только для ваших покупок.')
            cat=c.execute('SELECT id,workspace_id FROM categories WHERE NOT archived AND lower(name)=lower(%s)',(name.strip(),)).fetchone()
            if not cat:
                return Reply('Категория не найдена. Название должно совпадать со списком при вводе.')
            self._upsert_rule(c,cat['workspace_id'],user_id,'keyword',pattern,cat['id'])
            return Reply('Личное правило сохранено. /rules — просмотр и отключение.')
        if command=='/rules':
            if arg and (not arg.isascii() or not arg.isdigit() or len(arg)>6 or int(arg)<1):
                return Reply('Используйте /rules или /rules 2.')
            page=int(arg or 1)
            rows=c.execute('SELECT r.*,cat.name FROM category_rules r JOIN categories cat ON cat.id=r.category_id WHERE r.enabled ORDER BY r.updated_at DESC,r.id LIMIT 6 OFFSET %s',((page-1)*5,)).fetchall()
            reply=Reply('Личные правила:\n' if rows else 'На этой странице нет правил. /rule — создать правило по словам.')
            for i,r in enumerate(rows[:5],start=1):
                kind='точное описание' if r['match_kind']=='description' else 'слова'
                reply.text+=f"\n{i}. {r['pattern']} → {r['name']} ({kind})\n"
                reply.buttons.append([(f'Отключить №{i}',f"disable:{r['id']}")])
            if len(rows)>5:
                reply.text+=f'\n/rules {page+1} — далее'
            if page>1:
                reply.text+=f'\n/rules {page-1} — назад'
            return reply
        return None
