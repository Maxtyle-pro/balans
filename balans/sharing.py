from uuid import UUID
from psycopg.types.json import Jsonb
from balans.domain import Reply
from balans.share_data import DEFAULT_OPTIONS,share_parts

class Sharing:
    def _share_get(self,c,identity):
        return c.execute('SELECT s.*,r.snapshot FROM share_drafts s JOIN reports r ON r.id=s.report_id WHERE s.id=%s AND s.expires_at>now() AND r.expires_at>now()',(UUID(str(identity)),)).fetchone()

    def _share_card(self,c,d):
        o=d['options'];parts=share_parts(d['snapshot'],o)
        buttons=[[(('✓ ' if o[key] else '○ ')+label,f"shtoggle:{key}:{d['id']}")] for key,label in [('income','Доходы'),('details','Список операций'),('date','Даты в списке'),('amount','Суммы в списке'),('category','Категории в списке'),('description','Описания в списке')]]
        categories=sorted({r['category'] for r in d['snapshot']['rows'] if r.get('kind','expense') in ('expense','refund')})
        page=o.get('category_page',0)
        for i,name in enumerate(categories[page*10:(page+1)*10],page*10):buttons.append([(('Исключена: ' if name in o['excluded'] else 'Включена: ')+name,f"shcategory:{i}:{d['id']}")])
        if (page+1)*10<len(categories):buttons.append([('Следующие категории',f"shpage:{page+1}:{d['id']}")])
        if page:buttons.append([('Предыдущие категории',f"shpage:{page-1}:{d['id']}")])
        buttons.append([('Подготовить к пересылке',f"shprepare:{d['id']}:{d['version']}")])
        return Reply('Предпросмотр копии ниже. По умолчанию передаётся только сводка; описания включаются отдельно. Отдельные поля счетов, контрагентов, внутренних ID и вложения не включаются; выбранные описания передаются как есть. Получатель сможет сохранить и переслать копию; отозвать её средствами бота нельзя.',buttons,messages=parts,access_workspace_id=str(d['workspace_id']))

    def _sharing_callback(self,c,user,callback):
        action,_,raw=callback.partition(':')
        if action not in ('rshare','shtoggle','shcategory','shpage','shprepare'):return None
        if action=='rshare':
            report=self._get_report(c,raw)
            if not report:return Reply('Отчёт недоступен или истёк.')
            row=c.execute('INSERT INTO share_drafts(workspace_id,author_user_id,report_id,options) VALUES(%s,%s,%s,%s) RETURNING id',(report['workspace_id'],user,report['id'],Jsonb(DEFAULT_OPTIONS))).fetchone()
            return self._share_card(c,self._share_get(c,row['id']))
        if action in ('shtoggle','shcategory','shpage'):key,_,identity=raw.partition(':');version=None
        else:identity,_,version=raw.partition(':')
        d=self._share_get(c,identity)
        if not d:return Reply('Копия недоступна или истекла.')
        if action=='shprepare':
            if int(version)!=d['version']:return Reply('Состав изменился. Подтвердите текущий предпросмотр.')
            if d['state']=='editing':c.execute("UPDATE share_drafts SET state='prepared',parts=%s WHERE id=%s",(Jsonb(share_parts(d['snapshot'],d['options'])),d['id']))
            return Reply('Копия подготовлена.',share_id=str(d['id']),access_workspace_id=str(d['workspace_id']))
        if d['state']=='prepared':return Reply('Эта копия уже подготовлена. Создайте новую через «Поделиться» в отчёте.')
        o=d['options']
        if action=='shpage':
            page=int(key)
            if not 0<=page<500:return Reply('Страница недоступна.')
            o['category_page']=page
        elif action=='shtoggle':
            if key not in ('income','details','date','amount','category','description'):return Reply('Поле недоступно.')
            o[key]=not o[key]
        else:
            names=sorted({r['category'] for r in d['snapshot']['rows'] if r.get('kind','expense') in ('expense','refund')});index=int(key)
            if not 0<=index<len(names):return Reply('Категория недоступна.')
            if names[index] in o['excluded']:o['excluded'].remove(names[index])
            else:o['excluded'].append(names[index])
        share_parts(d['snapshot'],o)  # Validate before persisting a too-large selection.
        c.execute('UPDATE share_drafts SET options=%s,version=version+1 WHERE id=%s',(Jsonb(o),d['id']))
        return self._share_card(c,self._share_get(c,d['id']))

    def _resolve_share(self,actor,identity):
        with self._actor_transaction(actor) as c:
            d=self._share_get(c,identity)
            if not d or d['state']!='prepared':return Reply('Копия недоступна, не подготовлена или срок истёк.')
            return Reply(d['parts'][0],messages=d['parts'][1:])
