"""Small server-rendered admin panel. No third-party scripts, no financial browsing by default."""
import asyncio
import hmac
from html import escape
from aiohttp import web
from urllib.parse import urlparse

SECTIONS={'dashboard':'Обзор','users':'Пользователи','tariff':'Тариф','payments':'Платежи','billing_audit':'Сверка платежей','jobs':'AI-задачи','ai':'Настройки AI','queue':'Входящая очередь','support':'Поддержка','diagnostics':'Разрешённая диагностика','content':'Тексты и категории','roles':'Роли','audit':'Аудит'}
CSS='''*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#172a32;font:15px/1.5 system-ui,sans-serif}aside{position:fixed;width:235px;inset:0 auto 0 0;padding:30px 22px;background:#173d37;color:white;overflow:auto}aside b{display:block;font-size:27px;margin-bottom:28px}aside a{display:block;color:#d1e4df;padding:9px 12px;text-decoration:none;border-radius:7px}aside a.active{background:#2b5a50;color:white}main{margin-left:235px;padding:42px;max-width:1700px}h1{font-size:30px;margin:0 0 8px}h2{font-size:19px}p.note{color:#5b6d74}.panel{background:white;border:1px solid #e0e7e8;border-radius:12px;padding:23px;margin:22px 0;overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;border-bottom:1px solid #edf0f2;padding:12px;vertical-align:top;max-width:330px;overflow-wrap:anywhere}th{color:#617078;font-size:12px}input,select,textarea{font:inherit;border:1px solid #bfcdd2;border-radius:7px;padding:10px;width:100%;max-width:630px;margin:5px 0 14px;display:block}textarea{min-height:90px}button{background:#19715b;color:white;border:0;border-radius:7px;padding:11px 19px;cursor:pointer;font:inherit}label{font-size:13px;font-weight:600;display:block}.message{background:#e4f3ec;padding:16px;border-radius:8px}.login{margin:8vh auto;max-width:530px;padding:32px}.login main{margin:0;padding:0}.small{font-size:12px;color:#617078}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{flex:1;min-width:155px;background:#f5f9f7;padding:18px;border-radius:9px}.card strong{display:block;font-size:25px}@media(max-width:850px){aside{position:static;width:auto}aside a{display:inline-block}aside b{margin-bottom:10px}main{margin:0;padding:20px}}'''


def layout(title,body,section='',session=None):
    nav=''.join(f'<a class="{"active" if k==section else ""}" href="/?section={k}">{v}</a>' for k,v in SECTIONS.items() if not session or session['role']!='support' or k in ('support','diagnostics'))
    return f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Баланс</title><style>{CSS}</style><body>'+ (f'<aside><b>Баланс</b><span>Управление сервисом</span>{nav}</aside><main>' if session else '<div class="login"><main>')+f'<h1>{escape(title)}</h1>{body}</main>'+('' if session else '</div>')+'</body></html>'


LABELS={'registered':'Зарегистрировано','active_30d':'Активны за 30 дней','paid_users':'Платившие пользователи','payments':'Платежи','net_stars':'Stars после возвратов','refunded_stars':'Возвращено Stars','refunds':'Возвраты','jobs':'AI-задачи','failed_jobs':'Ошибки AI','input_tokens':'Входные токены','output_tokens':'Выходные токены','conversion_percent':'Конверсия в оплату, %','renewals':'Продления','expired_payers':'Платившие с истёкшим сроком','estimated_ai_usd':'Оценка AI за месяц, USD','telegram_user_id':'Telegram ID','status':'Статус','created_at':'Создано','last_seen':'Последняя активность','suspended':'Приостановлен','manual_until':'Ручной доступ до','paid_until':'Оплачен до','reason':'Причина','action':'Действие','state':'Состояние'}


def table(rows):
    if not rows:return '<p class="note">Пока нет записей.</p>'
    keys=list(rows[0]);head=''.join(f'<th>{escape(LABELS.get(k,str(k)))}</th>' for k in keys)
    body=''.join('<tr>'+''.join('<td>'+escape(str(row.get(k,'')))+'</td>' for k in keys)+'</tr>' for row in rows)
    return '<table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table>'


def field(name,label,kind='text',value=''):
    return f'<label>{escape(label)}<input name="{name}" type="{kind}" value="{escape(str(value),quote=True)}" required></label>'


def form(action,fields,session,title='Сохранить'):
    return '<form method="post" action="/action">'+f'<input type="hidden" name="action" value="{action}"><input type="hidden" name="csrf" value="{session["csrf"]}">'+fields+field('reason','Причина изменения')+'<label>Код TOTP (если с проверки прошло больше 5 минут)<input name="otp" inputmode="numeric" autocomplete="one-time-code"></label>'+f'<button>{title}</button></form>'


@web.middleware
async def security(request,handler):
    try:response=await handler(request)
    except web.HTTPException as exc:response=exc
    except ValueError as exc:response=web.Response(text=layout('Не удалось выполнить действие','<p>'+escape(str(exc))+'</p><a href="/">Вернуться в панель</a>'),content_type='text/html',status=400)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
    if isinstance(response,web.HTTPException):raise response
    return response


def create_app(service,bot=None):
    app=web.Application(middlewares=[security],client_max_size=20000)
    base=service.admin_config.base_url
    async def session(request):
        value=await asyncio.to_thread(service.admin_session,request.cookies.get('balans_admin',''))
        if not value:raise web.HTTPFound('/login')
        return value
    async def login(request):
        if not service.admin_config.enabled:raise web.HTTPNotFound()
        if request.method=='POST':
            if request.headers.get('Origin') and request.headers['Origin']!=base:raise web.HTTPForbidden()
            values=await request.post();token=await asyncio.to_thread(service.admin_login,values.get('code',''),values.get('otp',''))
            if not token:raise ValueError('Код неверен, использован или истёк. Получите новый через /admin в боте.')
            response=web.HTTPFound('/');response.set_cookie('balans_admin',token,httponly=True,secure=urlparse(base).scheme=='https',samesite='Strict',max_age=1800,path='/');return response
        return web.Response(text=layout('Вход в Баланс','<p class="note">Получите одноразовый код командой /admin в Telegram-боте. Второй код — из вашего приложения аутентификации.</p><div class="panel"><form method="post">'+field('code','Код из Telegram')+field('otp','Код TOTP')+'<button>Войти</button></form></div><p class="small">Только разрешённые аккаунты. Все изменения протоколируются.</p>'),content_type='text/html')
    async def home(request):
        s=await session(request);section=request.query.get('section','dashboard');rows=await asyncio.to_thread(service.admin_data,s['telegram_user_id'],section,request.query.get('search',''))
        body=f'<p class="note">Telegram ID {s["telegram_user_id"]} · {escape(s["role"])}. Личные операции пользователей здесь не открываются.</p>'
        if request.query.get('message'):body+='<p class="message">'+escape(request.query['message'])+'</p>'
        if section=='users':body+='<form><input type="hidden" name="section" value="users">'+field('search','Поиск по Telegram ID')+'<button>Найти</button></form>'
        if section=='dashboard':body+='<div class="panel cards">'+''.join('<div class="card">'+escape(LABELS.get(k,k))+'<strong>'+escape(str(v) if v is not None else '—')+'</strong></div>' for k,v in rows[0].items())+'</div><p class="small">Стоимость — оценка по заданным ставкам, а не счёт провайдера. Когорты конверсии: все зарегистрированные и все когда-либо платившие.</p>'
        else:body+='<div class="panel">'+table(rows)+'</div>'
        f=''
        if section=='users':
            f='<h2>Изменить доступ</h2>'+''.join(form(action,field('target','Telegram ID')+(field('days','Дней доступа','number','30') if action=='grant' else ''),s,label) for action,label in [('grant','Выдать доступ'),('suspend','Приостановить'),('unsuspend','Возобновить')])
        elif section=='tariff' and s['role']=='owner':
            row=rows[0];f='<h2>Новая версия тарифа</h2>'+form('tariff',''.join(field(k,label,'number' if k in ('stars','trial_days','text_quota','voice_seconds','image_quota') else 'text',row[k] or '') for k,label in [('stars','Цена Stars'),('trial_days','Пробный период, дней'),('text_quota','Текстовых AI-запросов / месяц'),('voice_seconds','Секунд голоса / месяц'),('image_quota','Файлов изображений / месяц'),('terms_url','Опубликованные условия HTTPS'),('support_contact','Контакт поддержки')]),s,'Сохранить и включить оплату')
        elif section=='support':f='<h2>Ответить на обращение</h2>'+form('support_reply',field('target','ID обращения')+'<label>Ответ<textarea name="reply" required></textarea></label>',s,'Сохранить ответ')
        elif section=='queue':f='<h2>Повторить неудачное событие</h2>'+form('queue_retry',field('target','bot_id:update_id'),s,'Повторить')
        elif section=='roles' and s['role']=='owner':f=form('role',field('target','Telegram ID')+'<label>Роль<select name="role"><option>support</option><option>admin</option><option>disabled</option></select></label>',s)
        elif section=='content' and s['role']=='owner':f=form('content','<label>Текст<select name="target"><option>help_intro</option><option>support_intro</option><option>base_categories</option></select></label><label>Значение<textarea name="value" required></textarea></label>',s)
        elif section=='ai' and s['role']=='owner':f='<p>Настройки моделей применяются после перезапуска бота.</p>'+form('ai',field('model','Модель',value=rows[0]['model'] or 'gpt-4.1-mini-2025-04-14')+field('transcribe_model','Модель речи',value=rows[0]['transcribe_model'] or 'gpt-4o-mini-transcribe')+field('confidence','Порог уверенности',value=rows[0]['confidence'] or '.75')+''.join('<label>'+label+'<input name="'+k+'" value="'+escape(str(rows[0][k] or ''))+'"></label>' for k,label in [('text_usd_per_request','Оценка USD за текстовый запрос'),('voice_usd_per_minute','Оценка USD за минуту голоса'),('image_usd_per_file','Оценка USD за файл'),('monthly_cost_limit_usd','Лимит оценочной стоимости USD в месяц (пусто — выключен)')]),s)
        elif section=='diagnostics':f='<form action="/diagnostic">'+field('id','ID согласованного доступа')+'<button>Открыть снимок</button></form>'
        elif section=='payments' and s['role']=='owner':f='<h2>Подготовить возврат Stars</h2>'+form('refund_prepare',field('target','Telegram charge_id'),s,'Предпросмотр возврата')
        elif section=='billing_audit' and s['role'] in ('owner','admin'):f=form('reconcile','',s,'Сверить с Telegram')
        if f:body+='<div class="panel">'+f+'</div>'
        body+='<form method="post" action="/logout"><input type="hidden" name="csrf" value="'+s['csrf']+'"><button>Выйти</button></form>'
        return web.Response(text=layout(SECTIONS.get(section,'Панель'),body,section,s),content_type='text/html')
    async def authenticated_post(request,step_up=True):
        s=await session(request);values=await request.post()
        if not hmac.compare_digest(values.get('csrf',''),s['csrf']):raise web.HTTPForbidden()
        if request.headers.get('Origin') and request.headers['Origin']!=base:raise web.HTTPForbidden()
        if step_up and not await asyncio.to_thread(service.admin_step_up,s,values.get('otp','')):raise ValueError('Для изменения введите свежий код TOTP.')
        return s,dict(values)
    async def action(request):
        s,v=await authenticated_post(request);actor=s['telegram_user_id'];a=v.get('action')
        if a in ('refund_prepare','refund_confirm','reconcile'):
            if s['role']!='owner':raise web.HTTPForbidden()
            await asyncio.to_thread(service.admin_log_external,actor,a,v.get('target',''),v.get('reason',''))
            if a=='refund_prepare':
                r=await asyncio.to_thread(service.request_refund,actor,v.get('target',''),v.get('reason',''))
                body='<p>Вернуть полную сумму платежа этому пользователю? Доступ по платежу прекратится. Автопродление меняется отдельно.</p>'+table([{k:r[k] for k in ('id','charge_id','telegram_user_id','stars','reason')}])+form('refund_confirm',f'<input type="hidden" name="target" value="{r["id"]}">',s,'Подтвердить возврат Stars')
                return web.Response(text=layout('Подтверждение возврата',body,'payments',s),content_type='text/html')
            if not bot:raise ValueError('Telegram API не подключён к панели.')
            if a=='refund_confirm':
                from balans.refunds import execute_refund
                message=await execute_refund(bot,service,actor,v['target'])
            else:
                from balans.star_reconciliation import reconcile_stars
                result=await reconcile_stars(bot,service);message=f'Сверка: {result}'
        else:message=await asyncio.to_thread(service.admin_mutate,actor,a,v)
        from urllib.parse import urlencode
        raise web.HTTPFound('/?'+urlencode({'message':message}))
    async def diagnostic(request):
        s=await session(request);data=await asyncio.to_thread(service.diagnostic_snapshot,s['telegram_user_id'],request.query.get('id',''))
        return web.Response(text=layout('Разрешённый снимок',table([data]),'diagnostics',s),content_type='text/html')
    async def logout(request):
        await authenticated_post(request,False);await asyncio.to_thread(service.admin_logout,request.cookies.get('balans_admin',''));r=web.HTTPFound('/login');r.del_cookie('balans_admin');return r
    app.router.add_get('/',home);app.router.add_get('/login',login);app.router.add_post('/login',login);app.router.add_post('/action',action);app.router.add_get('/diagnostic',diagnostic);app.router.add_post('/logout',logout)
    return app


async def start_admin(service,bot):
    if not service.admin_config.enabled:return None
    runner=web.AppRunner(create_app(service,bot),access_log=None);await runner.setup()
    import os
    # Public HTTPS terminates at a separately configured reverse proxy.
    await web.TCPSite(runner,os.getenv('ADMIN_HOST','127.0.0.1'),int(os.getenv('ADMIN_PORT','8088'))).start()
    return runner
