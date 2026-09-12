"""Numbered history: selections refer to the displayed snapshot, never a fresh page."""
from uuid import UUID
from psycopg.types.json import Jsonb
from balans.domain import Reply, money


class HistoryUI:
    def _history_page(self,c,page):
        rows=c.execute('SELECT o.id,o.created_by_user_id=actor_user_id() AS editable,o.kind,o.state,r.amount,r.currency,r.description,r.occurred_on,coalesce(cat.name,chr(8212)) AS category FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id LEFT JOIN categories cat ON cat.id=r.category_id ORDER BY o.created_at DESC,o.id DESC LIMIT 6 OFFSET %s',((page-1)*5,)).fetchall()
        if not rows:return Reply('На этой странице записей нет.',[[('В начало истории','history'),('Меню','ui:menu')]])
        from balans.finance import KINDS
        lines=[f"{i}. {r['description'] or KINDS[r['kind']]} — {money(r['amount'],r['currency'])}\n{r['occurred_on']:%d.%m.%Y} · {r['category']}"+(' · Отменена' if r['state']=='cancelled' else '') for i,r in enumerate(rows[:5],1)]
        token=c.execute("INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s) RETURNING id",(Jsonb({'action':'history_select','page':page,'ids':[str(r['id']) for r in rows[:5]]}),)).fetchone()['id']
        buttons=[[('✏️ Изменить расход',f'hselect:{token}')]]
        nav=[]
        if page>1:nav.append(('← Назад',f'ui:go:history:{page-1}'))
        if len(rows)>5:nav.append(('Далее →',f'ui:go:history:{page+1}'))
        if nav:buttons.append(nav)
        buttons.append([('Меню','ui:menu')])
        return Reply(f'📋 Ваши операции · страница {page}\n\n'+'\n\n'.join(lines),buttons)

    def _history_entry(self,c,user,text,sent,callback):
        if callback and callback.startswith('hselect:'):
            if self._workspace_busy(c) or self._input_batch(c):return Reply('Сначала завершите текущий ввод.',[[('Продолжить','ui:resume')]])
            token=UUID(callback.split(':')[1])
            row=c.execute("SELECT payload FROM fund_confirmations WHERE id=%s AND author_user_id=%s AND workspace_id=current_workspace() AND expires_at>now()",(token,user)).fetchone()
            if not row or row['payload'].get('action')!='history_select':return Reply('Этот список устарел. Откройте историю заново.',[[('История','history')]])
            c.execute("INSERT INTO ui_inputs(user_id,workspace_id,action) VALUES(%s,current_workspace(),%s) ON CONFLICT(user_id) DO UPDATE SET action=excluded.action,workspace_id=excluded.workspace_id,expires_at=now()+interval '30 minutes',id=gen_random_uuid()",(user,'history:'+str(token)))
            return Reply(f"Какой расход хотите изменить? Напишите номер из списка выше — от 1 до {len(row['payload']['ids'])}.",[[('Назад к списку',f"ui:go:history:{row['payload']['page']}")]])
        if callback or text.startswith('/'):return None
        pending=c.execute("SELECT * FROM ui_inputs WHERE user_id=%s AND action LIKE 'history:%%'",(user,)).fetchone()
        if not pending:return None
        token=UUID(pending['action'].split(':')[1])
        row=c.execute("SELECT payload FROM fund_confirmations WHERE id=%s AND author_user_id=%s AND workspace_id=current_workspace() AND expires_at>now() AND %s>now() AND %s=current_workspace()",(token,user,pending['expires_at'],pending['workspace_id'])).fetchone()
        if not row:
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            return Reply('Этот список устарел. Откройте историю заново.',[[('История','history')]])
        ids=row['payload']['ids'];page=row['payload']['page']
        if not text.isascii() or not text.isdigit() or len(text)>2 or not 1<=int(text)<=len(ids):return Reply(f'Напишите номер от 1 до {len(ids)}.',[[('Назад к списку',f'ui:go:history:{page}')]])
        c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
        reply=self._finance_callback(c,user,'fedit:'+ids[int(text)-1],sent)
        draft=self._draft(c)
        if draft and str(draft['edit_operation_id'])==ids[int(text)-1]:
            c.execute("INSERT INTO fund_confirmations(workspace_id,author_user_id,payload) VALUES(current_workspace(),actor_user_id(),%s)",(Jsonb({'action':'history_edit','draft':str(draft['id']),'page':page}),))
            reply.text='Что изменить?\n\n'+reply.text
        return reply
