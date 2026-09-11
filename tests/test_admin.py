import asyncio
import time
from itertools import count
import psycopg
import pytest
from balans.admin_auth import AdminConfig,totp,digest
from balans.admin_web import create_app
from balans.service import Service
from test_service import send,query,draft
from test_receipts import button

USERS=count(160000000)
SECRET='JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'

@pytest.fixture
def admin(database):
    actor=next(USERS)
    with psycopg.connect(database[0]) as c:c.execute("INSERT INTO balans.admin_roles VALUES(%s,'owner',true)",(actor,))
    s=Service(database[1],admin_config=AdminConfig(True,'http://127.0.0.1:8088',{actor:SECRET}));send(s,actor,'/start')
    yield s,actor
    s.close()
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.admin_roles SET active=false WHERE telegram_user_id=%s',(actor,))


def test_login_one_time_totp_and_role_revocation(admin,database):
    s,actor=admin;code=s.issue_admin_code(actor)
    assert not s.admin_login(code,'000000')
    token=s.admin_login(code,totp(SECRET,int(time.time())//30));assert token
    assert not s.admin_login(code,totp(SECRET,int(time.time())//30))
    session=s.admin_session(token);assert session['telegram_user_id']==actor
    assert s.admin_step_up(session)
    with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.admin_roles SET active=false WHERE telegram_user_id=%s',(actor,))
    assert not s.admin_session(token)


def test_admin_metadata_not_financial_access(admin,database):
    s,actor=admin;user=next(USERS);send(s,user,callback=draft(s,user,'123'))
    assert s.admin_data(actor,'users',str(user))[0]['telegram_user_id']==user
    assert not query(database,actor,'SELECT id FROM operations')
    with pytest.raises(ValueError):s.admin_data(user,'users')
    assert not query(database,user,'SELECT * FROM service_users')
    s.admin_mutate(actor,'grant',{'target':str(user),'days':'30','reason':'Тестовая компенсация'})
    assert s.admin_data(actor,'audit')[0]['action']=='grant'
    assert 'Одноразовый' in send(s,actor,'/admin').text


def test_diagnostic_explicit_revocable_snapshot(admin,database):
    s,actor=admin;user=next(USERS);send(s,user,callback=draft(s,user,'345'))
    op=query(database,user,'SELECT id FROM operations')[0][0]
    r=send(s,user,f'/diagnostic {op}')
    grant=button(r,'Разрешить на 15 минут').split(':')[1]
    with pytest.raises(ValueError):s.diagnostic_snapshot(actor,grant)
    send(s,user,callback=button(r,'Разрешить на 15 минут'))
    assert s.diagnostic_snapshot(actor,grant)['amount'].startswith('345')
    assert s.admin_data(actor,'audit')[0]['action']=='diagnostic_view'
    send(s,user,f'/diagnostic revoke {grant}')
    with pytest.raises(ValueError):s.diagnostic_snapshot(actor,grant)
    assert not query(database,actor,'SELECT * FROM operations')


def test_content_support_and_roles(admin,database):
    s,actor=admin;user=next(USERS)
    s.admin_mutate(actor,'content',{'target':'help_intro','value':'Добро пожаловать','reason':'Обновление помощи'})
    assert send(s,user,'/help').text=='Добро пожаловать'
    send(s,user,'/support Не вижу отчёт')
    ticket=query(database,user,'SELECT id FROM support_tickets')[0][0]
    s.admin_mutate(actor,'support_reply',{'target':str(ticket),'reply':'Откройте /report','reason':'Ответ на обращение'})
    assert 'Откройте /report' in send(s,user,'/support').text
    s.admin_mutate(actor,'role',{'target':str(user),'role':'support','reason':'Назначение поддержки'})
    assert s.admin_data(user,'support')
    with pytest.raises(ValueError):s.admin_data(user,'payments')
    with pytest.raises(ValueError):s.admin_mutate(user,'grant',{'target':str(user),'days':'30','reason':'Нет полномочий'})
    with psycopg.connect(database[0]) as c:c.execute("DELETE FROM balans.service_content WHERE key='help_intro'")


def test_web_auth_csrf_and_escaping(admin):
    from aiohttp.test_utils import TestClient,TestServer
    from aiohttp import CookieJar
    s,actor=admin
    async def run():
        async with TestClient(TestServer(create_app(s)),cookie_jar=CookieJar(unsafe=True)) as client:
            r=await client.get('/');assert r.url.path=='/login'
            code=s.issue_admin_code(actor)
            r=await client.post('/login',data={'code':code,'otp':totp(SECRET,int(time.time())//30)})
            assert r.status==200 and 'Обзор' in await r.text()
            r=await client.post('/action',data={'action':'grant','target':'1','reason':'Без CSRF'})
            assert r.status==403
            token=client.session.cookie_jar.filter_cookies(client.make_url('/'))['balans_admin'].value
            session=s.admin_session(token)
            r=await client.get('/?message=<script>alert(1)</script>')
            body=await r.text();assert '<script>' not in body and '&lt;script&gt;' in body
            assert r.headers['Cache-Control']=='no-store'
            r=await client.post('/logout',data={'csrf':session['csrf']});assert r.url.path=='/login'
            assert not s.admin_session(token)
    asyncio.run(run())


def test_defaults_and_suspend_without_billing(admin,database):
    s,actor=admin;user=next(USERS)
    s.admin_mutate(actor,'content',{'target':'base_categories','value':'Инструменты','reason':'Каталог категорий'})
    send(s,user,'/start')
    assert query(database,user,"SELECT name FROM categories WHERE name='Инструменты'")==[('Инструменты',)]
    s.admin_mutate(actor,'suspend',{'target':str(user),'reason':'Проверка ограничений'})
    assert 'завершён' in send(s,user,'/manual 100').text
    assert 'нет' in send(s,user,'/history').text.lower()
    s.admin_mutate(actor,'unsuspend',{'target':str(user),'reason':'Проверка завершена'})
    assert 'завершён' not in send(s,user,'/manual 100').text
    with psycopg.connect(database[0]) as c:c.execute("DELETE FROM balans.service_content WHERE key='base_categories'")


def test_cost_cap_applies_without_commercial_billing(admin,database):
    from test_categorization import FakeAI
    s,actor=admin;u=next(USERS)
    try:
        s.admin_mutate(actor,'ai',{'reason':'Ограничение тестовой стоимости','model':'','transcribe_model':'','confidence':'.75','text_usd_per_request':'.10','voice_usd_per_minute':'.01','image_usd_per_file':'.01','monthly_cost_limit_usd':'.05'})
        s.ai=FakeAI();send(s,u,'/start')
        with s._actor_transaction(u) as c:c.execute('UPDATE user_settings SET ai_enabled=true WHERE user_id=actor_user_id()')
        send(s,u,'/add 10');r=send(s,u,'Кофе')
        assert 'стоимости' in r.text and not s.ai.calls
    finally:
        with psycopg.connect(database[0]) as c:c.execute('UPDATE balans.ai_configuration SET monthly_cost_limit_usd=NULL,model=NULL,transcribe_model=NULL,confidence=NULL')
