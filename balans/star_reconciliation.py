"""Recover only matching invoice transactions from Telegram's authoritative Stars history."""
from datetime import timedelta
from balans.billing import PERIOD


def transaction_payment(transaction):
    partner=transaction.source or transaction.receiver
    refund=transaction.receiver is not None
    if not partner or partner.type!='user' or partner.transaction_type!='invoice_payment' or not partner.invoice_payload:return None
    if not refund and partner.subscription_period!=PERIOD:return None
    return partner.user.id,{'invoice_payload':partner.invoice_payload,'telegram_payment_charge_id':transaction.id,'currency':'XTR','total_amount':abs(transaction.amount),'is_recurring':True,'subscription_expiration_date':transaction.date+timedelta(seconds=PERIOD)},refund


async def reconcile_stars(bot,service,max_pages=100):
    import asyncio
    offset=0;seen=set();matched=0;review=0
    for _ in range(max_pages):
        page=await bot.get_star_transactions(offset=offset,limit=100)
        for transaction in page.transactions:
            parsed=transaction_payment(transaction)
            if not parsed:continue
            actor,payment,refund=parsed;key=(transaction.id,refund)
            if key in seen:continue
            seen.add(key)
            reply=await asyncio.to_thread(service.reconcile_payment,actor,payment,refund)
            if 'подтверждена' in reply.text or 'Возврат Stars учтён' in reply.text:matched+=1
            else:review+=1
        if len(page.transactions)<100:return {'matched':matched,'review':review,'complete':True}
        offset+=len(page.transactions)
    return {'matched':matched,'review':review,'complete':False}
