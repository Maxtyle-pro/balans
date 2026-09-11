"""Explicit operator refund requests; exposed only behind the admin authentication layer."""
import asyncio
from uuid import UUID

class Refunds:
    def request_refund(self,admin,charge,reason):
        if not 3<=len(reason.strip())<=1000:raise ValueError('Укажите причину возврата (3–1000 символов).')
        with self._actor_transaction(admin) as c:
            if c.execute('SELECT owner_telegram_id FROM billing_config').fetchone()['owner_telegram_id']!=admin:raise ValueError('Недостаточно прав.')
            c.execute("SELECT set_config('balans.billing_worker','on',true)")
            p=c.execute('SELECT * FROM billing_payments WHERE charge_id=%s AND success_seen',(charge,)).fetchone()
            if not p or p['refunded']:raise ValueError('Платёж недоступен или уже возвращён.')
            result=c.execute('INSERT INTO billing_refund_requests(charge_id,user_id,telegram_user_id,requested_by,reason) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(charge_id) DO UPDATE SET charge_id=excluded.charge_id RETURNING *',(charge,p['user_id'],p['telegram_user_id'],admin,reason.strip())).fetchone()
            return dict(result,stars=p['stars'])

    def claim_refund(self,admin,identity):
        with self._actor_transaction(admin) as c:
            row=c.execute("UPDATE billing_refund_requests SET state='running' WHERE id=%s AND state='pending' RETURNING *",(UUID(str(identity)),)).fetchone()
            if not row:raise ValueError('Запрос уже выполнялся. Проверьте историю Stars перед повтором.')
            c.execute("SELECT set_config('balans.billing_worker','on',true)")
            payment=c.execute('SELECT * FROM billing_payments WHERE charge_id=%s',(row['charge_id'],)).fetchone()
            return row,payment

    def finish_refund(self,admin,identity,state):
        if state not in ('succeeded','uncertain'):raise ValueError('Недопустимое состояние.')
        with self._actor_transaction(admin) as c:c.execute('UPDATE billing_refund_requests SET state=%s WHERE id=%s',(state,UUID(str(identity))))


async def execute_refund(bot,service,admin,identity):
    row,p=await asyncio.to_thread(service.claim_refund,admin,identity)
    try:
        await bot.refund_star_payment(row['telegram_user_id'],row['charge_id'])
        await asyncio.to_thread(service.record_payment,row['telegram_user_id'],{'invoice_payload':str(p['invoice_id']),'telegram_payment_charge_id':p['charge_id'],'currency':'XTR','total_amount':p['stars']},True)
    except Exception:
        await asyncio.to_thread(service.finish_refund,admin,identity,'uncertain')
        return 'Результат возврата требует сверки с Telegram. Автоматического повтора нет.'
    await asyncio.to_thread(service.finish_refund,admin,identity,'succeeded')
    return 'Stars возвращены; доступ по платежу прекращён. Настройка автопродления не изменена.'
