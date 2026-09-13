from datetime import timedelta
from itertools import count
import psycopg
from test_service import send,query,draft
from test_receipts import button
from test_billing_demo import enable,checkout
from test_privacy import private_service,op,attach

USERS=count(310000000)

def owner_sql(db,sql,args=()):
    with psycopg.connect(db[0]) as c:
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        for t in ('documents','receipt_files','retention_batches','notification_outbox','quota_reservations'):c.execute(f'ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY')
        result=c.execute(sql,args)
        rows=result.fetchall() if result.description else []
        for t in ('documents','receipt_files','retention_batches','notification_outbox','quota_reservations'):c.execute(f'ALTER TABLE {t} FORCE ROW LEVEL SECURITY')
        return rows


def test_ninety_days_and_no_delete_without_delivery(private_service,database):
    s=private_service;u=next(USERS);identity=op(s,u,database);attach(s,u,identity)
    doc,days=query(database,u,'SELECT id,extract(epoch FROM expires_at-created_at)/86400 FROM documents')[0]
    assert 89.9<=days<=90.1
    owner_sql(database,"UPDATE documents SET expires_at=now()-interval '1 day' WHERE id=%s",(doc,))
    s.purge_private_files();assert s.document_storage.personal.path(doc).exists()
    s.plan_notifications()
    notices=query(database,u,"SELECT id,entity_id FROM notification_outbox WHERE kind='retention'")
    assert len(notices)==1
    notice,batch=notices[0]
    owner_sql(database,"UPDATE notification_outbox SET state='sending' WHERE id=%s",(notice,))
    assert s.prepare_notification(u,notice)
    s.finish_notification(u,notice,'sent')
    assert query(database,u,'SELECT retention_warned_at IS NOT NULL,expires_at>now() FROM documents WHERE id=%s',(doc,))==[(True,True)]
    s.plan_notifications();assert len(query(database,u,"SELECT id FROM notification_outbox WHERE kind='retention'"))==1
    archive=send(s,u,callback=f'storagezip:{batch}');assert archive.generated_document
    owner_sql(database,"UPDATE documents SET expires_at=now()-interval '1 day',retention_warned_at=now()-interval '8 days' WHERE id=%s",(doc,))
    s.purge_private_files();assert not s.document_storage.personal.path(doc).exists()
    assert query(database,u,'SELECT count(*) FROM operations')==[(1,)]


def test_demo_quota_pack_idempotent_and_period_scoped(service,database):
    s=service;u=enable(s,database);pay=checkout(s,u);send(s,u,callback=button(pay,'Оплатить тестово'))
    before=query(database,u,"SELECT (billing_access()->'quotas'->>'image')::integer")[0][0]
    offer=send(s,u,callback='addon:image');assert 'Условная цена' in offer.text and not offer.invoice_id
    callback=button(offer,'Оплатить тестово')
    send(s,u,callback=callback);send(s,u,callback=callback)
    assert query(database,u,"SELECT (billing_access()->'quotas'->>'image')::integer")==[(before+50,)]
    assert not query(database,u,'SELECT charge_id FROM billing_payments')
    other=next(USERS);assert 'недоступно' in send(s,other,callback=callback).text
    assert 'Реальные цены' in send(s,other,callback='addon:image').text


def test_storage_demo_extension_preserves_file(private_service,database):
    s=private_service;u=enable(s,database);pay=checkout(s,u);send(s,u,callback=button(pay,'Оплатить тестово'))
    identity=op(s,u,database);attach(s,u,identity);doc=query(database,u,'SELECT id FROM documents')[0][0]
    owner_sql(database,"UPDATE documents SET expires_at=now()+interval '6 days' WHERE id=%s",(doc,))
    s.plan_notifications();batch=query(database,u,'SELECT id FROM retention_batches')[0][0]
    before=query(database,u,'SELECT expires_at FROM documents')[0][0]
    offer=send(s,u,callback=f'storagebuy:{batch}');callback=button(offer,'Оплатить тестово')
    send(s,u,callback=callback);send(s,u,callback=callback)
    assert timedelta(days=90)<=query(database,u,'SELECT expires_at FROM documents')[0][0]-before<timedelta(days=91)
    assert query(database,u,'SELECT extended_at IS NOT NULL FROM retention_batches')==[(True,)]
    s.purge_private_files();assert s.document_storage.personal.path(doc).exists()


def test_quota_warnings_once_and_topup_unblocks(service,database):
    from test_categorization import FakeAI
    s=service;s.ai=FakeAI();u=enable(s,database)
    pay=checkout(s,u);send(s,u,callback=button(pay,'Оплатить тестово'))
    query(database,u,"UPDATE billing_demo SET paid_quotas=jsonb_set(paid_quotas,'{text}','5') RETURNING user_id")
    send(s,u,'/ai on')
    for _ in range(5):
        send(s,u,'Кофе 250');send(s,u,'/cancel')
    notices=query(database,u,"SELECT message FROM notification_outbox WHERE kind='quota' ORDER BY created_at")
    assert len(notices)==2 and '20%' in notices[0][0] and 'исчерпан' in notices[1][0]
    count=len(s.ai.calls)
    blocked=send(s,u,'Кофе 250');assert 'Лимит ИИ исчерпан' in blocked.text and len(s.ai.calls)==count
    assert button(blocked,'Добавить пакет')=='addon:text'
    offer=send(s,u,callback='addon:text');send(s,u,callback=button(offer,'Оплатить тестово'))
    send(s,u,'Кофе 250');assert len(s.ai.calls)==count+1


def test_upgrade_does_not_stack_and_demo_reset_revokes(service,database):
    s=service;u=enable(s,database);pay=checkout(s,u);send(s,u,callback=button(pay,'Оплатить тестово'))
    baseline=query(database,u,"SELECT (billing_access()->'quotas'->>'text')::integer")[0][0]
    first=send(s,u,callback='addon:upgrade');second=send(s,u,callback='addon:upgrade')
    send(s,u,callback=button(first,'Оплатить тестово'))
    assert 'уже подключён' in send(s,u,callback=button(second,'Оплатить тестово')).text
    assert query(database,u,"SELECT (billing_access()->'quotas'->>'text')::integer")==[(baseline*2,)]
    panel=send(s,u,'/demo');send(s,u,callback=button(panel,'Новый пользователь'))
    pay=checkout(s,u);send(s,u,callback=button(pay,'Оплатить тестово'))
    assert query(database,u,"SELECT (billing_access()->'quotas'->>'text')::integer")==[(baseline,)]


def test_shared_files_notify_owner_and_expire_after_warning(private_service,database):
    from test_workspaces import admit
    s=private_service;owner,member=next(USERS),next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,member)
    identity=op(s,member,database);attach(s,member,identity)
    doc=query(database,owner,'SELECT id FROM documents')[0][0]
    assert s.document_storage.shared.path(doc).exists()
    owner_sql(database,"UPDATE documents SET expires_at=now()+interval '6 days' WHERE id=%s",(doc,))
    s.plan_notifications()
    notice,batch=query(database,owner,"SELECT id,entity_id FROM notification_outbox WHERE kind='retention'")[0]
    assert not query(database,member,"SELECT id FROM notification_outbox WHERE kind='retention'")
    assert send(s,owner,callback=f'storagezip:{batch}').generated_document
    assert 'недоступ' in send(s,member,callback=f'storagezip:{batch}').text
    owner_sql(database,"UPDATE notification_outbox SET state='sending' WHERE id=%s",(notice,))
    s.finish_notification(owner,notice,'sent')
    owner_sql(database,"UPDATE documents SET expires_at=now()-interval '1 day',retention_warned_at=now()-interval '8 days' WHERE id=%s",(doc,))
    s.purge_private_files()
    assert not s.document_storage.shared.path(doc).exists()
    assert query(database,owner,'SELECT count(*) FROM operations')==[(1,)]
