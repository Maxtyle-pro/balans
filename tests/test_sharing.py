from itertools import count
from uuid import UUID
import base64
from test_service import send,query,draft
from test_receipts import button
from test_workspaces import admit
from test_funds import issue,confirm,identity
from balans.share_data import share_parts,DEFAULT_OPTIONS
from scripts.check_report_pdf import fixture

USERS=count(130000000);UPDATES=count(2200000)

def test_share_whitelist_and_selected_categories():
    s=fixture();parts=share_parts(s,DEFAULT_OPTIONS);text='\n'.join(parts)
    assert '41 175,00' in text
    for row in s['rows']:
        assert row['id'] not in text and row['description'] not in text and row['merchant'] not in text
    options={**DEFAULT_OPTIONS,'excluded':['Продукты'],'details':True,'description':True}
    text='\n'.join(share_parts(s,options))
    assert 'Выборочный' in text and 'Продукты' not in text
    assert s['rows'][1]['description'] in text
    assert all(len(p)<=3500 for p in share_parts(s,options))


def test_prepared_copy_is_immutable_and_has_no_actions(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'111'))
    report=send(s,u,'/report');preview=send(s,u,callback=button(report,'Поделиться'))
    assert preview.messages and 'Тестовый расход' not in '\n'.join(preview.messages)
    ready=send(s,u,callback=button(preview,'Подготовить к пересылке'))
    assert '111,00' in ready.text and not ready.buttons and not ready.document_ids
    send(s,u,callback=draft(s,u,'222'))
    repeated=send(s,u,callback=button(preview,'Подготовить к пересылке'))
    assert repeated.text==ready.text and repeated.messages==ready.messages
    assert '333,00' not in repeated.text


def test_old_preview_and_copy_revoked_after_leaving(service,database):
    s=service;owner,u=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,u);send(s,u,callback=draft(s,u,'444'))
    report=send(s,u,'/report')
    spaces=send(s,u,'/workspaces');send(s,u,callback=button(spaces,'Личный бюджет'))
    update=next(UPDATES);preview=send(s,u,callback=button(report,'Поделиться'),update=update)
    ready=send(s,u,callback=button(preview,'Подготовить к пересылке'))
    assert '444,00' in ready.text
    members=send(s,owner,'/members');r=send(s,owner,callback=button(members,'Исключить '+str(u)))
    send(s,owner,callback=button(r,'Подтвердить прекращение доступа'))
    assert not send(s,u,callback=button(report,'Поделиться'),update=update).messages
    assert '444,00' not in send(s,u,callback=button(preview,'Подготовить к пересылке')).text


def test_toggle_requires_current_preview(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    preview=send(s,u,callback=button(send(s,u,'/report'),'Поделиться'))
    toggle=next(data for row in preview.buttons for label,data in row if 'Список операций' in label)
    changed=send(s,u,callback=toggle)
    assert 'Состав изменился' in send(s,u,callback=button(preview,'Подготовить к пересылке')).text
    toggle=next(data for row in changed.buttons for label,data in row if 'Описания в списке' in label)
    changed=send(s,u,callback=toggle)
    assert 'Тестовый расход' in '\n'.join(changed.messages)


def test_shared_report_member_scope_and_revision_snapshot(service,database):
    s=service;owner,u,other=next(USERS),next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,u);admit(s,owner,other)
    issue(s,owner,u,'20000');tr=identity(database,owner)
    confirm(s,u,send(s,u,f'/receive {tr} | 20000'))
    send(s,u,callback=draft(s,u,'6500'));send(s,other,callback=draft(s,other,'900'))
    card=send(s,owner,f'/report all | member={u}')
    report=query(database,owner,'SELECT snapshot FROM reports ORDER BY created_at DESC LIMIT 1')[0][0]
    assert report['summary']['total']=='6500.000000'
    assert len(report['funds']['participants'])==1
    assert report['funds']['participants'][0]['closing']=='13500.000000'
    assert all(r['participant']==str(u) for r in report['rows'])
    assert '900,00' not in card.text
    operation=str(query(database,u,"SELECT id FROM operations WHERE kind='expense'")[0][0])
    edit=send(s,u,callback='fedit:'+operation)
    send(s,u,callback=button(edit,'Сумма'));edit=send(s,u,'6000');send(s,u,callback=button(edit,'Сохранить изменения'))
    audit=send(s,owner,callback=button(card,'PDF с графиками').replace('rpdf:','raudit:'))
    raw=base64.b64decode(audit.generated_document).decode()
    assert '6500' in raw and '6000' not in raw


def test_excluded_purchase_category_excludes_its_refund(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    op=str(query(database,u,'SELECT id FROM operations')[0][0])
    r=send(s,u,callback='frefund:'+op);r=send(s,u,'50');send(s,u,callback=button(r,'Подтвердить'))
    menu=send(s,u,callback='opcat:'+op);send(s,u,callback=button(menu,'Дом'))
    report=send(s,u,'/report');preview=send(s,u,callback=button(report,'Поделиться'))
    toggle=next(data for row in preview.buttons for label,data in row if label=='Включена: Дом')
    preview=send(s,u,callback=toggle)
    text='\n'.join(preview.messages)
    assert 'Чистые расходы: 0,00' in text and 'Возвраты покупок: 0,00' in text


def test_long_period_uses_monthly_buckets():
    from datetime import date
    from balans.report_data import summarize
    result=summarize([{'date':'2024-01-20','amount':'100','category':'Дом'}],date(2024,1,1),date(2026,9,10))
    assert result['granularity']=='month' and len(result['days'])==33
    assert result['days'][0]['total']=='100'
