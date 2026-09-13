"""Durable polling/webhook intake with bounded asynchronous workers."""
import asyncio
import base64
from datetime import datetime, timezone
import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
import psycopg

from balans.domain import Reply
from balans.service import Service
from balans.notifications import notification_loop
from balans.support_delivery import support_loop
from balans.inbox import inbox_loop
from balans.admin_web import start_admin
from balans.privacy import privacy_loop
from balans.ai import CategoryAI
from balans.receipt_media import BoundedBuffer, MAX_FILE_BYTES, MediaError

log = logging.getLogger('balans')
COMMANDS = [('start','Открыть главное меню')]


async def process_update(bot, service, update):
    if update.pre_checkout_query:
        q=update.pre_checkout_query
        ok=await asyncio.to_thread(service.pre_checkout,q.from_user.id,q.id,q.invoice_payload,q.currency,q.total_amount)
        await bot.answer_pre_checkout_query(q.id,ok=ok,error_message=None if ok else 'Счёт недействителен или уже оплачен. Откройте /subscription заново.')
        return
    query = update.callback_query
    message = query.message if query else update.message
    sender = query.from_user if query else (message.from_user if message else None)
    if not message or not sender or sender.is_bot or message.chat.type != 'private' or message.chat.id != sender.id:
        return
    await asyncio.to_thread(service.notifications_blocked,sender.id,False)
    if not query and (message.successful_payment or message.refunded_payment):
        payment=message.successful_payment or message.refunded_payment
        reply=await asyncio.to_thread(service.record_payment,sender.id,payment.model_dump(),bool(message.refunded_payment))
        await deliver_reply(bot,message.chat.id,reply,service)
        return
    # Plain text: names/descriptions are never interpreted as Telegram HTML/Markdown.
    if not query and message.text is None:
        if await asyncio.to_thread(service.awaiting_contact,sender.id):
            await deliver_reply(bot,message.chat.id,Reply('Напишите обращение текстом — так я смогу передать его разработчику.',[[('Отмена','ui:menu')]]),service)
            return
        if message.voice:
            reply=await receive_voice_file(bot,service,update,message,sender)
            await deliver_reply(bot,message.chat.id,reply,service)
        elif message.photo or message.document:
            reply=await receive_file(bot,service,update,message,sender)
            await deliver_reply(bot,message.chat.id,reply,service)
        else:
            await deliver_reply(bot,message.chat.id,Reply('Доступны голосовые сообщения, фото/PDF чеков и ручной ввод. Выберите способ:',[[('🎙 Голос','ui:go:voice'),('📷 Чек','ui:go:receipts')],[('✍️ Вручную','ui:go:manual'),('☰ Все действия','ui:menu')]]),service)
        return
    if query:
        try:
            await bot.answer_callback_query(query.id)
        except TelegramBadRequest:
            pass  # An expired callback can still have an idempotently processed save.
    reply = await asyncio.to_thread(service.handle, sender.id, bot.id, update.update_id,
                                   message.text or '', datetime.now(timezone.utc) if query else message.date,
                                   query.data if query else None)
    await deliver_reply(bot,message.chat.id,reply,service)


async def deliver_reply(bot,chat_id,reply,service=None):
    from balans.command_ui import present_reply
    reply=present_reply(reply)
    if reply.invoice_id and service:
        invoice=await asyncio.to_thread(service.prepare_invoice,chat_id,reply.invoice_id)
        if not invoice:
            await deliver_reply(bot,chat_id,Reply('Счёт истёк или оплата выключена.',[[('⭐ Моя подписка','subscription')]]),service);return
        from aiogram.types import LabeledPrice
        url=invoice['invoice_url']
        if not url:
            url=await bot.create_invoice_link(title='Баланс — 30 дней',description='Личный и совместный учёт. Автопродление каждые 30 дней. Квоты и условия показаны перед оплатой.',payload=str(invoice['id']),provider_token='',currency='XTR',prices=[LabeledPrice(label='Подписка на 30 дней',amount=invoice['stars'])],subscription_period=2592000)
            await asyncio.to_thread(service.cache_invoice_url,chat_id,reply.invoice_id,url)
        await bot.send_message(chat_id,f"{invoice['stars']} Stars за 30 дней с автопродлением. Условия: {invoice['terms_url']}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Оплатить Stars',url=url)]]));return
    if reply.renewal_invoice_id and service:
        invoice=await asyncio.to_thread(service.renewal_details,chat_id,reply.renewal_invoice_id)
        if not invoice:
            await bot.send_message(chat_id,'Подписка недоступна.');return
        await bot.edit_user_star_subscription(chat_id,invoice['first_charge_id'],is_canceled=not reply.renewal_enabled)
        await asyncio.to_thread(service.record_renewal,chat_id,reply.renewal_invoice_id,reply.renewal_enabled)
        await bot.send_message(chat_id,'Автопродление включено.' if reply.renewal_enabled else 'Автопродление отменено. Оплаченный срок сохранён.');return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in reply.buttons]) if reply.buttons else None
    if reply.text:await bot.send_message(chat_id,reply.text,reply_markup=keyboard,parse_mode=reply.parse_mode)
    for extra in reply.additional_replies:
        await deliver_reply(bot,chat_id,Reply(**extra),service)
    for part in reply.messages:
        await deliver_reply(bot,chat_id,Reply(part),service)
    if reply.generated_document:
        await bot.send_document(chat_id,BufferedInputFile(base64.b64decode(reply.generated_document),filename=reply.generated_filename))
    for file_id in reply.photo_ids:
        try:
            await bot.send_photo(chat_id,file_id)
        except TelegramBadRequest:
            await bot.send_message(chat_id,'Telegram больше не выдаёт этот исходник. Данные расхода сохранены.')
    for file_id in reply.document_ids:
        try:
            await bot.send_document(chat_id,file_id)
        except TelegramBadRequest:
            await bot.send_message(chat_id,'Telegram больше не выдаёт этот исходник. Данные расхода сохранены.')


async def receive_voice_file(bot,service,update,message,sender):
    gate=await asyncio.to_thread(service.voice_preflight,sender.id,bot.id,update.update_id,message.voice.duration)
    if gate:
        return gate
    voice=message.voice
    async def error(text):
        return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,text)
    if voice.duration>180 or (voice.file_size and voice.file_size>MAX_FILE_BYTES):
        return await error('Голосовое сообщение должно быть до 3 минут и 15 МБ.')
    try:
        remote=await bot.get_file(voice.file_id)
        if not remote.file_path or (remote.file_size and remote.file_size>MAX_FILE_BYTES):
            return await error('Голосовой файл недоступен или больше 15 МБ.')
        with BoundedBuffer() as buffer:
            await bot.download_file(remote.file_path,destination=buffer,timeout=30)
            data=buffer.getvalue()
    except (TelegramBadRequest, MediaError, asyncio.TimeoutError):
        return await error('Не удалось скачать голосовое сообщение до 15 МБ. Отправьте его заново.')
    return await asyncio.to_thread(service.receive_voice,sender.id,bot.id,update.update_id,message.date,data)


async def receive_file(bot,service,update,message,sender):
    async def result(reply):
        if reply.receipt_job_id:
            if message.media_group_id:
                # The durable inbox serializes an actor. Let the last album file trigger OCR.
                await asyncio.sleep(.7)
                if await asyncio.to_thread(service.album_has_more,bot.id,sender.id,update.update_id,message.media_group_id):
                    return Reply('')
            return await asyncio.to_thread(service._resolve_receipt,sender.id,reply.receipt_job_id)
        return reply
    gate=await asyncio.to_thread(service.receipt_preflight,sender.id,bot.id,update.update_id)
    if gate:
        return await result(gate)
    attachment=message.photo[-1] if message.photo else message.document
    if message.document:
        document=message.document
        supported=document.mime_type in ('application/pdf','image/jpeg','image/png') or (document.file_name or '').lower().endswith(('.pdf','.jpg','.jpeg','.png'))
        if not supported:
            return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,'Нужен PDF, JPEG или PNG. Другие форматы пока не поддерживаются.')
    if attachment.file_size and attachment.file_size>MAX_FILE_BYTES:
        return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,'Файл больше 15 МБ. Отправьте меньший файл.')
    try:
        remote=await bot.get_file(attachment.file_id)
    except TelegramBadRequest:
        return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,'Telegram не выдал файл. Отправьте PDF/JPEG/PNG размером до 15 МБ заново.')
    if not remote.file_path or (remote.file_size and remote.file_size>MAX_FILE_BYTES):
        return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,'Файл недоступен или больше 15 МБ.')
    try:
        with BoundedBuffer() as buffer:
            await bot.download_file(remote.file_path,destination=buffer,timeout=30)
            reply=await asyncio.to_thread(service.receive_receipt,sender.id,bot.id,update.update_id,message.date,attachment.file_id,buffer.getvalue(),'photo' if message.photo else 'document')
            return await result(reply)
    except MediaError as exc:
        return await asyncio.to_thread(service.receipt_error,sender.id,bot.id,update.update_id,str(exc))


async def polling(bot,service,stop):
    offset=None;delay=1
    while not stop.is_set():
        try:
            updates=await bot.get_updates(offset=offset,timeout=20,allowed_updates=['message','callback_query','pre_checkout_query'])
            for update in updates:
                if update.pre_checkout_query:await process_update(bot,service,update)
            await asyncio.to_thread(service.accept_updates,bot.id,[u for u in updates if not u.pre_checkout_query])
            if updates:offset=updates[-1].update_id+1
            delay=1
        except Exception as exc:
            log.error('Ошибка приёма (%s); повтор через %s с',type(exc).__name__,delay)
            try:await asyncio.wait_for(stop.wait(),exc.retry_after if isinstance(exc,TelegramRetryAfter) else delay)
            except TimeoutError:pass
            delay=min(delay*2,30)


async def main():
    import signal
    from balans.transport import start_webhook
    load_dotenv()
    token=os.getenv('BOT_TOKEN','').strip();dsn=os.getenv('DATABASE_URL','').strip()
    if not token or not dsn:raise SystemExit('Заполните BOT_TOKEN и DATABASE_URL в локальном .env. См. README.md.')
    workers=int(os.getenv('INBOX_WORKERS','4'))
    if not 1<=workers<=16:raise SystemExit('INBOX_WORKERS должен быть от 1 до 16.')
    mode=os.getenv('TELEGRAM_TRANSPORT','polling')
    if mode not in ('polling','webhook'):raise SystemExit('TELEGRAM_TRANSPORT: polling или webhook.')
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
    service=Service(dsn,os.getenv('SUPPORT_CONTACT','').strip(),ai=CategoryAI.from_env())
    try:
        await asyncio.to_thread(service.check)
        async with Bot(token) as bot:
            me=await bot.get_me()
            with psycopg.connect(dsn,autocommit=True) as lock:
                if not lock.execute('SELECT pg_try_advisory_lock(%s)',(-bot.id,)).fetchone()[0]:raise SystemExit('Этот бот уже запущен с данной БД.')
                if mode=='polling' and (await bot.get_webhook_info()).url:raise SystemExit('У бота настроен webhook. Отключите его явно, сохранив pending updates.')
                admin_runner=None;webhook_runner=None;inboxes=[];background=[]
                try:
                    await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in COMMANDS])
                    admin_runner=await start_admin(service,bot)
                    if mode=='webhook':webhook_runner=await start_webhook(bot,service,process_update)
                    background=[asyncio.create_task(privacy_loop(service)),asyncio.create_task(notification_loop(bot,service)),asyncio.create_task(support_loop(bot,service))]
                    inboxes=[asyncio.create_task(inbox_loop(bot,service,process_update,stop)) for _ in range(workers)]
                    log.info('Бот @%s запущен (%s, обработчиков: %s)',me.username,mode,workers)
                    if mode=='polling':await polling(bot,service,stop)
                    else:await stop.wait()
                finally:
                    stop.set()
                    if webhook_runner:await webhook_runner.cleanup()
                    if admin_runner:await admin_runner.cleanup()
                    # Keep Bot and DB alive until active jobs commit. SIGKILL recovery uses leases.
                    await asyncio.gather(*inboxes,return_exceptions=True)
                    for task in background:task.cancel()
                    await asyncio.gather(*background,return_exceptions=True)
                    await loop.shutdown_default_executor()
    finally:service.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
