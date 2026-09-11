from uuid import UUID
from psycopg.errors import RaiseException
from balans.domain import Reply


class Workspaces:
    def _workspace_name(self,c):
        row=c.execute('SELECT name FROM workspaces WHERE id=current_workspace()').fetchone()
        return row['name'] if row else 'Бюджет недоступен'

    def _workspace_call(self,c,statement,args=()):
        try:
            with c.transaction():return c.execute(statement,args).fetchone()
        except RaiseException as exc:raise ValueError(exc.diag.message_primary) from None

    def _workspace_busy(self,c):
        return self._media_queue(c) or self._document_upload(c) or self._draft(c) or self._input_batch(c) or self._voice_busy(c) or c.execute("SELECT id FROM receipt_batches WHERE state IN ('collecting','processing')").fetchone() or c.execute("SELECT id FROM ai_jobs WHERE state='running' AND lease_until>now()").fetchone()

    def _workspace_list(self,c):
        rows=c.execute('SELECT id,name,kind,owner_user_id=actor_user_id() AS owner,id=current_workspace() AS selected FROM workspaces ORDER BY kind,name,id').fetchall()
        return Reply('Ваши бюджеты\n'+'\n'.join(('✓ ' if r['selected'] else '')+r['name']+(' · руководитель' if r['kind']=='shared' and r['owner'] else ' · участник' if r['kind']=='shared' else ' · личный') for r in rows)+'\n/workspace Название — создать совместный бюджет\n/invite — приглашение\n/members — участники и заявки\n/join КОД — вступить', [[(r['name'],f"wsuse:{r['id']}")] for r in rows])

    def _workspace_command(self,c,user,command,arg,sent):
        if command=='/workspaces':return self._workspace_list(c)
        if command=='/workspace':
            if self._workspace_busy(c):return Reply('Завершите текущий ввод перед созданием бюджета: /cancel, /batch cancel, /receipts.')
            if not arg:raise ValueError('/workspace Название — новый совместный бюджет.')
            self._workspace_call(c,'SELECT create_workspace(%s)',(arg,))
            return self._workspace_list(c)
        if command=='/invite':
            row=self._workspace_call(c,'SELECT new_invitation() AS token')
            return Reply('Одноразовое приглашение в «'+self._workspace_name(c)+'», срок — 24 часа. Передайте участнику эту команду:\n/join '+str(row['token'])+'\nПосле его согласия подтвердите Telegram ID в /members. До подтверждения доступа к бюджету нет.')
        if command=='/join':
            token=UUID(arg.strip());row=self._workspace_call(c,'SELECT join_workspace(%s,false) AS info',(token,))['info']
            return Reply(f"Приглашение в «{row['name']}». Руководитель: Telegram ID {row['owner']}.\nРуководитель видит операции, остаток и документы этого общего бюджета. Ваш личный бюджет остаётся закрытым. Участники не видят операции друг друга.", [[('Согласиться и запросить доступ',f'wsjoin:{token}')]])
        if command=='/members':
            members=c.execute("SELECT *,owner_user_id=actor_user_id() AS managed FROM memberships WHERE workspace_id=current_workspace() ORDER BY role,telegram_user_id").fetchall()
            pending=c.execute("SELECT id,claimant_telegram_id FROM workspace_invitations WHERE workspace_id=current_workspace() AND owner_user_id=actor_user_id() AND state='pending' AND expires_at>now()").fetchall()
            buttons=[]
            for row in pending:buttons.append([(f"Принять {row['claimant_telegram_id']}",f"wsapprove:{row['id']}"),(f"Отклонить {row['claimant_telegram_id']}",f"wsreject:{row['id']}")])
            invitations=c.execute("SELECT id,state FROM workspace_invitations WHERE workspace_id=current_workspace() AND owner_user_id=actor_user_id() AND state IN ('open','pending') AND expires_at>now() ORDER BY created_at").fetchall()
            for i,inv in enumerate(invitations,1):buttons.append([(f"Отозвать приглашение {i}",f"wsrevoke:{inv['id']}")])
            for m in members:
                if m['role']=='participant' and m['status']=='active':buttons.append([(('Исключить '+str(m['telegram_user_id']) if m['managed'] else 'Выйти из бюджета'),f"wsremove:{m['id']}")])
            return Reply('Участники: '+self._workspace_name(c)+'\n'+'\n'.join(f"Telegram ID {m['telegram_user_id']} · {m['role']} · {m['status']}" for m in members)+'\nЗаявки требуют подтверждения руководителем после сверки Telegram ID.',buttons)
        return None

    def _workspace_callback(self,c,user,callback,sent):
        action,_,raw=callback.partition(':')
        if action not in ('wsuse','wsjoin','wsapprove','wsreject','wsremove','wsremoveok','wsrevoke'):return None
        identity=UUID(raw)
        if action=='wsuse':
            if self._workspace_busy(c):return Reply('Сначала завершите или отмените текущий ввод. Он закреплён за прежним бюджетом.')
            if not c.execute('SELECT id FROM workspaces WHERE id=%s',(identity,)).fetchone():return Reply('Бюджет недоступен.')
            c.execute('UPDATE user_settings SET selected_workspace_id=%s,default_account_id=NULL WHERE user_id=%s',(identity,user))
            return self._workspace_list(c)
        if action=='wsjoin':
            self._workspace_call(c,'SELECT join_workspace(%s,true)',(identity,))
            return Reply('Запрос отправлен на проверку в бюджет. Руководитель должен подтвердить ваш Telegram ID в /members. После подтверждения бюджет появится в /workspaces.')
        if action in ('wsapprove','wsreject'):
            self._workspace_call(c,'SELECT decide_invitation(%s,%s)',(identity,action=='wsapprove'))
            return Reply('Заявка подтверждена. Участник может выбрать бюджет в /workspaces.' if action=='wsapprove' else 'Заявка отклонена.')
        if action=='wsrevoke':
            self._workspace_call(c,'SELECT revoke_invitation(%s)',(identity,))
            return Reply('Приглашение отозвано.')
        if action=='wsremove':
            m=c.execute("SELECT id,telegram_user_id FROM memberships WHERE id=%s AND workspace_id=current_workspace() AND role='participant' AND status='active'",(identity,)).fetchone()
            if not m:return Reply('Участник недоступен.')
            return Reply(f"Прекратить доступ участника {m['telegram_user_id']}? Общая история и документы останутся у руководителя.",[[('Подтвердить прекращение доступа',f'wsremoveok:{identity}')]])
        self._workspace_call(c,'SELECT remove_membership(%s)',(identity,))
        return Reply('Доступ прекращён. Общая история сохранена. /workspaces — доступные бюджеты.')
