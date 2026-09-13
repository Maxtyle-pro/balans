"""Category management without command syntax in user-facing messages."""
from uuid import UUID
from balans.domain import Reply

ICONS={'Одежда':'👕','Дом':'🏠','Другое':'📦','Без категории':'⚠️','Здоровье':'💊','Кафе и рестораны':'☕','Покупки':'🛍','Продукты':'🛒','Развлечения':'🎉','Транспорт':'🚕'}

def label(name):return (ICONS[name]+' ' if name in ICONS else '')+name

class CategorySettings:
    def _category_settings(self,c,page=1,mode='list'):
        page=max(1,page)
        rows=c.execute('SELECT id,name,archived FROM categories WHERE workspace_id=current_workspace() AND (%s OR archived=%s) ORDER BY archived,name LIMIT 9 OFFSET %s',(mode in ('list','rename'),mode=='restore',(page-1)*8)).fetchall()
        shown=rows[:8]
        titles={'list':'🏷 Категории','rename':'✏️ Какую категорию переименовать?','archive':'🙈 Какую категорию скрыть?','restore':'👁 Какую категорию вернуть?'}
        text=titles[mode]
        buttons=[]
        if mode=='list':
            text+='\n\n'+('\n'.join(label(r['name'])+(' · скрыта' if r['archived'] else '') for r in shown) or 'Пока нет категорий.')
            text+='\n\nСкрытые категории остаются в истории, но не предлагаются для новых записей.'
            buttons=[[('➕ Добавить','ui:go:categories_add')],[('✏️ Переименовать','catpage:rename:1')],[('🙈 Скрыть','catpage:archive:1')]]
            if c.execute('SELECT 1 FROM categories WHERE workspace_id=current_workspace() AND archived LIMIT 1').fetchone():buttons.append([('👁 Вернуть скрытую','catpage:restore:1')])
        else:
            if not shown:text+='\nНет подходящих категорий.'
            buttons=[[(label(r['name']),f"catselect:{mode}:{r['id']}")] for r in shown]
        nav=[]
        if page>1:nav.append(('← Назад',f'catpage:{mode}:{page-1}'))
        if len(rows)>8:nav.append(('Далее →',f'catpage:{mode}:{page+1}'))
        if nav:buttons.append(nav)
        buttons.append([('← Настройки','ui:go:settings')] if mode=='list' else [('← К списку','ui:go:categories')])
        return Reply(text,buttons,command_hints=False)

    def _category_settings_entry(self,c,user,text,callback):
        if callback in ('ui:go:categories_rename','ui:go:categories_archive'):
            return self._category_settings(c,mode='rename' if callback.endswith('rename') else 'archive')
        if callback and callback.startswith('catpage:'):
            _,mode,page=callback.split(':')
            if mode not in ('list','rename','archive','restore'):return Reply('Действие недоступно.')
            c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
            return self._category_settings(c,int(page),mode)
        if callback and callback.startswith('catselect:'):
            _,mode,identity=callback.split(':')
            row=c.execute('SELECT name FROM categories WHERE id=%s AND workspace_id=current_workspace()',(UUID(identity),)).fetchone()
            if not row:return Reply('Категория недоступна.',[[('← К списку','ui:go:categories')]])
            if mode=='rename':
                c.execute("INSERT INTO ui_inputs(user_id,workspace_id,action) VALUES(%s,current_workspace(),%s) ON CONFLICT(user_id) DO UPDATE SET action=excluded.action,workspace_id=excluded.workspace_id,expires_at=now()+interval '30 minutes'",(user,'catrename:'+identity))
                return Reply('✏️ '+label(row['name'])+'\n\nНапишите новое название. Можно добавить свой эмодзи, например «🐶 Питомцы».',[[('Отмена','ui:go:categories')]],command_hints=False)
            if mode in ('archive','restore'):
                c.execute('SELECT manage_category(%s,%s)',(mode,row['name']))
                return self._category_settings(c)
        if not callback and not text.startswith('/'):
            pending=c.execute("SELECT action FROM ui_inputs WHERE user_id=%s AND action LIKE 'catrename:%%' AND expires_at>now() AND workspace_id=current_workspace()",(user,)).fetchone()
            if pending:
                row=c.execute('SELECT name FROM categories WHERE id=%s AND workspace_id=current_workspace()',(UUID(pending['action'].split(':')[1]),)).fetchone()
                if not row:return Reply('Категория недоступна.',[[('← К списку','ui:go:categories')]])
                c.execute("SELECT manage_category('rename',%s,%s)",(row['name'],text.strip()))
                c.execute('DELETE FROM ui_inputs WHERE user_id=%s',(user,))
                return self._category_settings(c)
        return None
