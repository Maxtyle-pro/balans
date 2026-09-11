from itertools import count
from decimal import Decimal
import pytest
from test_service import send,query,draft
from test_receipts import button

USERS=count(99000000)


def invitation(s,owner):
    reply=send(s,owner,'/invite')
    return reply.text.split('/join ')[1].split('\n')[0]


def admit(s,owner,user):
    token=invitation(s,owner)
    preview=send(s,user,'/join '+token)
    send(s,user,callback=button(preview,'Согласиться и запросить доступ'))
    members=send(s,owner,'/members')
    send(s,owner,callback=button(members,'Принять '+str(user)))
    spaces=send(s,user,'/workspaces')
    return send(s,user,callback=button(spaces,'Закупки'))


def test_two_phase_membership_and_private_isolation(service,database):
    s=service;owner=next(USERS);user=next(USERS);other=next(USERS)
    send(s,user,callback=draft(s,user,'111'))
    send(s,owner,'/workspace Закупки')
    token=invitation(s,owner);preview=send(s,user,'/join '+token)
    assert 'личный бюджет остаётся закрытым' in preview.text
    send(s,user,callback=button(preview,'Согласиться и запросить доступ'))
    assert all(label!='Закупки' for row in send(s,user,'/workspaces').buttons for label,_ in row)
    assert 'недоступно' in send(s,other,'/join '+token).text
    members=send(s,owner,'/members');send(s,owner,callback=button(members,'Принять '+str(user)))
    spaces=send(s,user,'/workspaces');send(s,user,callback=button(spaces,'Закупки'))
    send(s,user,callback=draft(s,user,'222'))
    assert '222,00' in send(s,owner,'/history').text
    assert '111,00' not in send(s,owner,'/history').text
    assert query(database,owner,'SELECT count(*) FROM operations')==[(1,)]
    assert 'Участник '+str(user) in send(s,user,'/accounts').text
    assert 'Средства руководителя' not in send(s,user,'/accounts').text
    admit(s,owner,other)
    assert '222,00' not in send(s,other,'/history').text
    assert query(database,other,'SELECT count(*) FROM postings')==[(0,)]


def test_remove_revokes_access_and_preserves_history(service,database):
    s=service;owner=next(USERS);user=next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    send(s,user,callback=draft(s,user,'300'))
    spaces=send(s,user,'/workspaces');old=button(spaces,'Закупки')
    members=send(s,owner,'/members');confirmation=send(s,owner,callback=button(members,'Исключить '+str(user)))
    send(s,owner,callback=button(confirmation,'Подтвердить прекращение доступа'))
    assert '300,00' in send(s,owner,'/history').text
    assert '300,00' not in send(s,user,'/history').text
    assert 'недоступен' in send(s,user,callback=old).text
    assert query(database,user,'SELECT * FROM postings')==[]


def test_switch_blocked_by_draft(service,database):
    s=service;owner=next(USERS)
    spaces=send(s,owner,'/workspace Закупки')
    personal=button(spaces,'Личный бюджет')
    save=draft(s,owner,'400')
    assert 'прежним бюджетом' in send(s,owner,callback=personal).text
    send(s,owner,callback=save)
    send(s,owner,callback=personal)
    assert '400,00' not in send(s,owner,'/history').text


def test_participant_cannot_manage_or_modify_others(service,database):
    s=service;owner=next(USERS);user=next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    send(s,owner,callback=draft(s,owner,'500'))
    identity=str(query(database,owner,'SELECT id FROM operations')[0][0])
    assert 'недоступна' in send(s,user,callback='fedit:'+identity).text
    assert 'руководите' in send(s,user,'/invite').text
    assert 'недоступен' in send(s,user,'/categories add Чужое').text
    assert 'недоступен' in send(s,user,'/account Чужой').text
    assert '500,00' not in send(s,user,'/report').text
    assert '500,00' in send(s,owner,'/report').text


def test_invitation_revocation_and_audit(service,database):
    s=service;owner=next(USERS);user=next(USERS)
    send(s,owner,'/workspace Закупки')
    token=invitation(s,owner)
    members=send(s,owner,'/members')
    send(s,owner,callback=button(members,'Отозвать приглашение 1'))
    assert 'недоступно' in send(s,user,'/join '+token).text
    admit(s,owner,user)
    assert query(database,owner,"SELECT new_status FROM workspace_events WHERE subject_user_id<>(SELECT actor_user_id()) AND event_type='membership' ORDER BY created_at,id")
    members=send(s,user,'/members')
    confirmation=send(s,user,callback=button(members,'Выйти из бюджета'))
    send(s,user,callback=button(confirmation,'Подтвердить прекращение доступа'))
    assert all(label!='Закупки' for row in send(s,user,'/workspaces').buttons for label,_ in row)
    assert query(database,owner,"SELECT count(*) FROM workspace_events WHERE new_status='removed'")==[(1,)]


def test_supervisor_cannot_post_from_participant_account(service,database):
    s=service;owner=next(USERS);user=next(USERS)
    send(s,owner,'/workspace Закупки');admit(s,owner,user)
    callback=draft(s,owner,'700')
    with s._actor_transaction(owner) as c:
        account=c.execute('SELECT a.id FROM accounts a JOIN memberships m ON m.id=a.responsible_membership_id WHERE m.telegram_user_id=%s',(user,)).fetchone()['id']
        c.execute("UPDATE operation_drafts SET account_id=%s WHERE state='pending'",(account,))
    assert 'недоступен' in send(s,owner,callback=callback).text
    assert query(database,owner,'SELECT count(*) FROM operations')==[(0,)]
