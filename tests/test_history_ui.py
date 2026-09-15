from itertools import count
from test_service import send, query, draft
from test_receipts import button
from balans.service import Service

USERS=count(290000000)

def test_number_selection_snapshot_restart_and_autosave(service,database):
    s=service;u=next(USERS)
    send(s,u,callback=draft(s,u,'100'))
    history=send(s,u,'/history')
    assert '1.' in history.text and '/history' not in history.text
    token=button(history,'✏️ Изменить операцию')
    send(s,u,callback=draft(s,u,'200'))
    send(s,u,callback=token)
    assert 'номер от 1 до 1' in send(s,u,'9').text
    other=next(USERS)
    assert 'устарел' in send(s,other,callback=token).text
    restarted=Service(database[1])
    try:card=send(restarted,u,'1')
    finally:restarted.close()
    assert '100,00' in card.text
    assert [label for row in card.buttons for label,_ in row]==['Изменить сумму','Изменить дату','Изменить описание','Изменить категорию','Отмена']
    send(s,u,callback=button(card,'Изменить сумму'))
    card=send(s,u,'150')
    assert [label for row in card.buttons for label,_ in row]==['✏️ Изменить']
    assert query(database,u,'SELECT sum(amount) FROM operation_revisions WHERE revision_no=1')[0][0]==300
    assert query(database,u,'SELECT sum(r.amount) FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id')[0][0]==350


def test_back_discards_edit_and_numbers_are_not_implicit(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    history=send(s,u,'/history')
    send(s,u,callback=button(history,'✏️ Изменить операцию'))
    card=send(s,u,'1')
    assert [label for row in card.buttons for label,_ in row]==['Изменить сумму','Изменить дату','Изменить описание','Изменить категорию','Отмена']
    send(s,u,callback=button(card,'Отмена'))
    assert query(database,u,"SELECT count(*) FROM operation_drafts WHERE state='pending'")==[(0,)]
    send(s,u,'3')
    assert query(database,u,"SELECT amount,edit_operation_id FROM operation_drafts WHERE state='pending'")==[(3,None)]


def test_category_change_autosaves(service,database):
    s=service;u=next(USERS);send(s,u,callback=draft(s,u,'100'))
    before=query(database,u,'SELECT r.category_id FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id')
    history=send(s,u,'/history');send(s,u,callback=button(history,'✏️ Изменить операцию'))
    card=send(s,u,'1');choices=send(s,u,callback=button(card,'Изменить категорию'))
    card=send(s,u,callback=button(choices,'Здоровье'))
    assert query(database,u,'SELECT cat.name FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id JOIN categories cat ON cat.id=r.category_id')==[('Здоровье',)]
    assert query(database,u,'SELECT count(*) FROM operation_revisions')==[(2,)]
