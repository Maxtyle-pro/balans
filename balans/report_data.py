"""Exact report arithmetic and explicitly bounded periods."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
import calendar
import csv
from io import StringIO

ZERO=Decimal('0')
MAX_ROWS=5000


def period(arg,today):
    if len(arg)>80:raise ValueError('Слишком длинный период отчёта.')
    arg=arg.strip().lower()
    if arg in ('сегодня','today'):
        start=end=today
    elif arg in ('вчера','yesterday'):
        start=end=today-timedelta(days=1)
    elif arg in ('неделя','week'):
        start=today-timedelta(days=today.weekday());end=today
    elif not arg or arg in ('месяц','month'):
        start=today.replace(day=1);end=today
    else:
        try:
            if len(arg)==7 and arg[4]=='-':
                start=date.fromisoformat(arg+'-01')
                end=min(today,start.replace(day=calendar.monthrange(start.year,start.month)[1]))
            else:
                a,b=arg.split()
                start=date.fromisoformat(a) if '-' in a else date(*map(int,a.split('.')[::-1]))
                end=date.fromisoformat(b) if '-' in b else date(*map(int,b.split('.')[::-1]))
        except (ValueError,TypeError,OverflowError):
            raise ValueError('Период: сегодня, вчера, неделя, 2026-09 или 01.09.2026 10.09.2026.') from None
    if start>end or end>today or (end-start).days>=366:
        raise ValueError('Период должен быть до 366 дней, без будущих дат.')
    # Calendar-month comparison for month-to-date; otherwise previous equal interval.
    try:
        if start.day==1 and (start.year,start.month)==(end.year,end.month):
            previous_end=start-timedelta(days=1)
            previous_start=previous_end.replace(day=1)
            previous_end=previous_end.replace(day=min(end.day,previous_end.day))
        else:
            previous_end=start-timedelta(days=1)
            previous_start=previous_end-timedelta(days=(end-start).days)
    except OverflowError:
        raise ValueError('Дата слишком ранняя для сравнения с предыдущим периодом.') from None
    return start,end,previous_start,previous_end


def amount(value):
    return Decimal(str(value))


def fmt(value):
    return f'{amount(value):,.2f}'.replace(',',' ').replace('.',',')


def summarize(rows,start,end):
    all_rows=rows
    rows=[r for r in rows if r.get('kind','expense')=='expense']
    income=sum((amount(r['amount']) for r in all_rows if r.get('kind')=='income'),ZERO)
    refunds=sum((amount(r['amount']) for r in all_rows if r.get('kind')=='refund'),ZERO)
    monthly=(end-start).days>=366
    categories=defaultdict(lambda:ZERO);days=defaultdict(lambda:ZERO)
    for row in rows:
        categories[row['category']]+=amount(row['amount'])
        days[row['date'][:7]+'-01' if monthly else row['date']]+=amount(row['amount'])
    total=sum(categories.values(),ZERO)
    all_days=[];current=start.replace(day=1) if monthly else start
    while current<=end:
        all_days.append({'date':current.isoformat(),'total':str(days[current.isoformat()])})
        current=(current.replace(day=28)+timedelta(days=4)).replace(day=1) if monthly else current+timedelta(days=1)
    return {'total':str(total),'count':len(rows),'income':str(income),'refunds':str(refunds),'net_expenses':str(total-refunds),'cash_flow':str(income-total+refunds),'operation_count':len(all_rows),
            'daily_average':str((total/Decimal((end-start).days+1)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
            'categories':[{'name':name,'total':str(value),'share':str((value*100/total).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if total else '0'} for name,value in sorted(categories.items(),key=lambda x:(-x[1],x[0]))],
            'days':all_days,'granularity':'month' if monthly else 'day'}


def export_rows(snapshot):
    rows=[['ID','Дата','Тип','Сумма','Валюта','Категория','Счёт','Описание','Продавец','Источник','Счёт получателя','Исходная покупка','Бюджет','Участник Telegram ID','Проверка','Версия','Документы','Статус получения','Подтверждено получение'],
            *[[r['id'],r['date'],{'expense':'Расход','income':'Доход','transfer':'Перевод','refund':'Возврат','opening':'Начальный остаток'}[r.get('kind','expense')],f"{amount(r['amount']):.2f}",r.get('currency',snapshot.get('currency','RUB')),r['category'],r.get('account','Основной'),r['description'],r['merchant'],r['source'],r.get('destination_account',''),r.get('refund_of',''),snapshot.get('workspace','Личный бюджет'),r.get('participant',''),r.get('review_status',''),r.get('revision',''),r.get('document_count',''),r.get('receipt_status',''),r.get('received_amount','')] for r in snapshot['rows']]]
    rows=[[cell for index,cell in enumerate(row) if index not in (6,10)] for row in rows]
    if snapshot.get('converted') or any(r.get('currency','RUB')!='RUB' for r in snapshot['rows']):
        rows[0]+= ['Исходная сумма','Исходная валюта','Курс','Дата курса','Источник курса']
        for cells,row in zip(rows[1:],snapshot['rows']):cells += [row.get('original_amount',row['amount']),row.get('original_currency',row.get('currency','RUB')),row.get('exchange_rate') or '',row.get('exchange_rate_on') or '',row.get('rate_source') or '']
    return rows


def csv_bytes(snapshot):
    stream=StringIO(newline='');writer=csv.writer(stream)
    for row in export_rows(snapshot):
        # Prefix risky text, including leading whitespace before spreadsheet formulas.
        writer.writerow(["'"+str(x) if str(x).lstrip().startswith(('=','+','-','@','\t','\r')) else x for x in row])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')
