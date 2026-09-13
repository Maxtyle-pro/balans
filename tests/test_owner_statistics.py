import psycopg
from test_service import send
from balans.admin_service import STATISTICS_OWNER_ID


def test_stats_private_and_revocable(service,database):
    owner=STATISTICS_OWNER_ID
    send(service,owner,'/start')
    send(service,499801,'/start')
    assert service.statistics_owner_enabled()
    result=send(service,owner,'/stats')
    assert 'Пользователей:' in result.text and 'За 24 часа:' in result.text
    assert 'Статистика бота' in send(service,owner,callback='ownerstats').text
    assert 'недоступна' in send(service,499801,'/stats').text
    assert 'недоступна' in send(service,499801,callback='ownerstats').text
    with psycopg.connect(database[0]) as c:
        c.execute('UPDATE balans.admin_roles SET active=false WHERE telegram_user_id=%s',(owner,))
    try:
        assert not service.statistics_owner_enabled()
        assert 'недоступна' in send(service,owner,'/stats').text
    finally:
        with psycopg.connect(database[0]) as c:
            c.execute('UPDATE balans.admin_roles SET active=true WHERE telegram_user_id=%s',(owner,))
