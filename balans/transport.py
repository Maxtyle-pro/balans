"""Optional authenticated webhook and local readiness endpoint."""
import asyncio,hmac,os,re
from aiohttp import web
from aiogram.types import Update


def make_app(bot,service,processor,secret):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',secret):raise ValueError('WEBHOOK_SECRET: 32–256 букв, цифр, _ или -.')
    async def incoming(request):
        if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token','').encode(),secret.encode()):raise web.HTTPForbidden()
        try:update=Update.model_validate(await request.json())
        except Exception:raise web.HTTPBadRequest() from None
        if update.pre_checkout_query:await processor(bot,service,update)
        else:await asyncio.to_thread(service.accept_updates,bot.id,[update])
        return web.Response(text='ok')
    async def ready(request):
        try:await asyncio.wait_for(asyncio.to_thread(service.queue_health,bot.id),2)
        except Exception:return web.Response(status=503,text='unavailable')
        return web.Response(text='ok')
    app=web.Application(client_max_size=1024*1024)
    app.router.add_post('/telegram',incoming);app.router.add_get('/ready',ready)
    return app

async def start_webhook(bot,service,processor):
    from urllib.parse import urlsplit
    url=os.getenv('WEBHOOK_URL','').strip();secret=os.getenv('WEBHOOK_SECRET','').strip()
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path!='/telegram' or parsed.query or parsed.fragment or parsed.username:raise ValueError('WEBHOOK_URL должен быть HTTPS URL с путём /telegram.')
    app=make_app(bot,service,processor,secret)
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    try:
        await web.TCPSite(runner,os.getenv('WEBHOOK_HOST','127.0.0.1'),int(os.getenv('WEBHOOK_PORT','8080'))).start()
        await bot.set_webhook(url,secret_token=secret,allowed_updates=['message','callback_query','pre_checkout_query'],drop_pending_updates=False)
    except BaseException:
        await runner.cleanup();raise
    return runner
