from test_privacy import private_service

from test_service import send,query,draft
from test_receipts import button


def test_clear_history_confirmation_and_isolation(service,database):
    user=391001;other=391002
    send(service,user,callback=draft(service,user,'123'))
    send(service,other,callback=draft(service,other,'456'))
    preview=send(service,user,callback='historyclear')
    assert query(database,user,'SELECT count(*) FROM operations')==[(1,)]
    confirm=button(preview,'🗑 Удалить всю историю')
    assert 'устарело' in send(service,other,callback=confirm).text
    result=send(service,user,callback=confirm)
    assert 'История очищена' in result.text
    assert query(database,user,'SELECT count(*) FROM operations')==[(0,)]
    assert query(database,other,'SELECT count(*) FROM operations')==[(1,)]
    send(service,user,callback=draft(service,user,'789'))
    send(service,user,callback=confirm)
    assert query(database,user,'SELECT count(*) FROM operations')==[(1,)]


def test_clear_history_removes_files_and_keeps_quota(private_service,database):
    from test_documents import op,attach
    from uuid import uuid4
    s=private_service;u=391003
    identity=op(s,u,database);attach(s,u,identity)
    doc=query(database,u,'SELECT id FROM documents')[0][0]
    path=s.document_storage.personal.path(doc)
    assert path.exists()
    import psycopg
    with psycopg.connect(database[0]) as c:
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(u),))
        c.execute("SELECT set_config('balans.billing_worker','on',true)")
        c.execute("INSERT INTO quota_reservations(job_id,kind,workspace_id,sponsor_id,author_user_id,units,period_start,state) VALUES(%s,'text',current_workspace(),actor_user_id(),actor_user_id(),1,current_date,'consumed')",(uuid4(),))
    preview=send(s,u,callback='historyclear')
    result=send(s,u,callback=button(preview,'🗑 Удалить всю историю'))
    assert 'История очищена' in result.text
    assert query(database,u,'SELECT count(*) FROM documents')==[(0,)]
    assert query(database,u,'SELECT units,state FROM quota_reservations')==[(1,'consumed')]
    s.purge_private_files()
    assert not path.exists()
