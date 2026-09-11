"""Synthetic report fixture only; never reads users' financial data."""
from datetime import date,timedelta,datetime,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from balans.report_data import summarize
from balans.report_pdf import render_pdf


def fixture():
    categories=['Продукты','Кафе и рестораны','Транспорт','Дом','Здоровье','Покупки','Развлечения','Другое']
    rows=[{'id':str(uuid4()),'date':f'2026-09-{i%10+1:02d}','amount':str(Decimal(i+1)*Decimal('137.25')),'category':categories[i%8],
           'description':'Тестовая покупка для проверки отчёта: продукты и бытовые товары','merchant':'Тестовый магазин','source':'manual'} for i in range(24)]
    summary=summarize(rows,date(2026,9,1),date(2026,9,10))
    previous=summarize([],date(2026,8,1),date(2026,8,10))
    return dict(start='2026-09-01',end='2026-09-10',previous_start='2026-08-01',previous_end='2026-08-10',timezone='Europe/Moscow',created_at='2026-09-10T18:00:00+00:00',rows=rows,summary=summary,previous=previous,delta=summary['total'],delta_percent=None)


if __name__=='__main__':
    path=Path('output/pdf/report-demo.pdf');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(render_pdf(fixture()))
    print(path.resolve())
