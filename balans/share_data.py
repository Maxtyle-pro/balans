"""Whitelist-only text copies: no account access, identifiers or attachments."""
from datetime import date
from balans.report_data import summarize,fmt
from balans.finance import KINDS

DEFAULT_OPTIONS={'income':True,'details':False,'date':True,'amount':True,'category':True,'description':False,'excluded':[]}


def share_parts(snapshot,options):
    if snapshot.get('currency_reports'):
        parts=[part for child in snapshot['currency_reports'] for part in share_parts(child,options)]
        if len(parts)>10:raise ValueError('Слишком большой отчёт; сократите период или выберите валюту.')
        return parts
    currency=snapshot.get('currency','RUB');symbol='₽' if currency=='RUB' else currency
    excluded=set(options.get('excluded',[]))
    rows=[r for r in snapshot['rows'] if r.get('kind','expense') in ('expense','refund','income') and (options.get('income',True) or r.get('kind','expense')!='income') and r['category'] not in excluded]
    summary=summarize(rows,date.fromisoformat(snapshot['start']),date.fromisoformat(snapshot['end']))
    title='Выборочный финансовый отчёт' if excluded or not options.get('income',True) or snapshot.get('filter_member') else 'Финансовый отчёт'
    lines=[title,f"{snapshot['start']} — {snapshot['end']} · {currency}",'Снимок на '+snapshot['created_at'],f"Расходы: {fmt(summary['total'])} {symbol}",f"Возвраты покупок: {fmt(summary['refunds'])} {symbol}",f"Чистые расходы: {fmt(summary['net_expenses'])} {symbol}"]
    if snapshot.get('converted'):lines.append('Суммы пересчитаны по сохранённым ручным курсам.')
    if options.get('income',True):lines.append(f"Доходы: {fmt(summary['income'])} {symbol}")
    lines+=['Расходы по категориям:']+[f"{c['name']}: {fmt(c['total'])} {symbol}" for c in summary['categories']]
    if not rows:lines.append('Нет включённых операций.')
    if options.get('details'):
        lines.append('Включённые операции:')
        for row in rows:
            fields=[KINDS[row.get('kind','expense')]]
            if options.get('date'):fields.append(row['date'])
            if options.get('amount'):fields.append(fmt(row['amount'])+' ₽')
            if options.get('category'):fields.append(row['category'])
            if options.get('description'):fields.append(row['description'])
            lines.append(' · '.join(fields))
    lines.append('Переводы внутри бюджета не включены в доходы и расходы. Учёт может быть неполным.')
    parts=[];current=''
    for line in lines:
        if len(line)>3300:raise ValueError('Слишком длинная строка. Отключите описание для передачи.')
        if len(current)+len(line)+1>3400:parts.append(current);current=line
        else:current+=('\n' if current else '')+line
    if current:parts.append(current)
    if len(parts)>10:raise ValueError('Копия превышает 10 сообщений. Сократите период или отключите детализацию/описания.')
    return [f'Часть {i}/{len(parts)}\n'+p for i,p in enumerate(parts,1)] if len(parts)>1 else parts
