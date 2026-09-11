"""Conservative local parsing; uncertain messages never create ledger entries."""
from decimal import Decimal
import re
from balans.domain import amount_from_text,date_from_text,CURRENCY

NUMBER=re.compile(r'(?<![\w.,])(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*(?:тыс\.?|тысяч(?:и)?|к)(?!\w))?(?![\w.,])',re.I)


def parse_items(text,sent,zone):
    currency=CURRENCY.get()
    patterns={'RUB':r'\b(?:руб(?:лей|ля|ль)?\.?|RUB)\b|₽','USD':r'\b(?:usd|доллар\w*)\b|\$','EUR':r'\b(?:eur|евро)\b|€'}
    if any(re.search(pattern,text,re.I) for code,pattern in patterns.items() if code!=currency):raise ValueError('Валюта сообщения не совпадает с выбранным счётом '+currency+'. Выберите подходящий счёт: /accounts.')
    text=re.sub(patterns[currency],' ',text,flags=re.I)
    if len(text)>2000:raise ValueError('Сообщение слишком длинное: до 2000 символов и 8 операций.')
    if re.search(r'\b(завтра|планирую|хочу|буду|хотел|не|вернули|возврат|перев[её]л|перевод)\b|\bне\s+(?:потратил|купил|оплатил)',text,re.I):
        raise ValueError('Для переводов используйте /transfer, для возврата — кнопку покупки в /history. В свободном тексте нужны совершённые расходы или доходы в валюте выбранного счёта.')
    parts=[p.strip() for p in re.split(r';|\n|,(?=\s*[А-Яа-яA-Za-z])',text) if p.strip()]
    if not 1<=len(parts)<=8:raise ValueError('Отправьте от 1 до 8 операций.')
    items=[]
    for part in parts:
        day='сегодня'
        dates=re.findall(r'\b(?:сегодня|вчера|\d{2}\.\d{2}\.\d{4})\b',part,re.I)
        if len(dates)>1:raise ValueError('В строке несколько дат. Уточните одну дату операции.')
        if dates:
            day=dates[0];part=part.replace(day,' ',1)
        matches=list(NUMBER.finditer(part))
        if len(matches)!=1:raise ValueError('Укажите одну сумму в каждой строке. Примеры: «Кофе 250 вчера», «Продукты 2,5 тыс». Несколько покупок разделяйте точкой с запятой.')
        match=matches[0];raw=match.group()
        thousand=bool(re.search(r'[а-як]',raw,re.I))
        numeric=re.sub(r'\s*(?:тыс\.?|тысяч(?:и)?|к)$','',raw,flags=re.I).strip()
        value=amount_from_text(numeric)
        if thousand:value=amount_from_text(format(value*1000,'f').rstrip('0').rstrip('.') if '.' in format(value*1000,'f') else format(value*1000,'f'))
        before=part[:match.start()]
        if before.rstrip().endswith(('-','+')):raise ValueError('Укажите положительную сумму без знака.')
        description=(before+' '+part[match.end():]).strip(' ,.-')
        description=re.sub(r'\b(?:руб(?:лей|ля|ль)?\.?|RUB)\b|₽',' ',description,flags=re.I)
        description=' '.join(description.split())
        if not description or len(description)>500:raise ValueError('Добавьте описание покупки, до 500 символов.')
        kind='income' if re.search(r'\b(?:зарплата|доход|получил[аи]?|премия)\b',description,re.I) else 'expense'
        items.append({'amount':str(value),'description':description,'date':date_from_text(day,sent,zone).isoformat(),'kind':kind})
    return items
