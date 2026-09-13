"""Portable Cyrillic PDF with vector charts; no user text is interpreted as markup."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from threading import Lock
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.graphics.shapes import Drawing, Rect, String, Line, PolyLine
from balans.report_data import fmt, amount

INK=colors.HexColor('#182B42');TEAL=colors.HexColor('#087F8C');MUTED=colors.HexColor('#586B7D')
LOCK=Lock()


def register_fonts():
    with LOCK:
        if 'Balans' not in pdfmetrics.getRegisteredFontNames():
            root=Path(__file__).with_name('fonts')
            pdfmetrics.registerFont(TTFont('Balans',str(root/'DejaVuSans.ttf')))
            pdfmetrics.registerFont(TTFont('BalansBold',str(root/'DejaVuSans-Bold.ttf')))


def category_chart(categories):
    if len(categories)>8:
        rest=categories[7:]
        categories=categories[:7]+[{'name':'Остальные категории','total':str(sum((amount(c['total']) for c in rest),amount(0))),'share':str(sum((amount(c['share']) for c in rest),amount(0)))}]
    height=max(65,len(categories)*30+15);d=Drawing(495,height)
    peak=max((float(amount(c['total'])) for c in categories),default=1) or 1
    for i,c in enumerate(categories):
        y=height-25-i*30
        d.add(String(0,y+7,(c['name'][:16]+'…'+c['name'][-8:] if len(c['name'])>25 else c['name']),fontName='Balans',fontSize=9,fillColor=INK))
        d.add(Rect(178,y,190*float(amount(c['total']))/peak,17,fillColor=TEAL,strokeColor=None))
        d.add(String(490,y+5,f"{fmt(c['total'])} ({c['share']}%)",textAnchor='end',fontName='Balans',fontSize=8,fillColor=INK))
    return d


def daily_chart(days,monthly=False):
    d=Drawing(495,180);values=days
    if len(days)>62:
        values=[{'date':days[i]['date'],'total':str(sum((amount(x['total']) for x in days[i:i+7]),amount(0)))} for i in range(0,len(days),7)]
    peak=max((float(amount(x['total'])) for x in values),default=1) or 1
    for fraction in (0,.5,1):
        y=30+125*fraction
        d.add(Line(85,y,490,y,strokeColor=colors.HexColor('#DCE5ED')))
        d.add(String(80,y-3,fmt(peak*fraction),textAnchor='end',fontName='Balans',fontSize=7,fillColor=MUTED))
    points=[]
    for i,value in enumerate(values):
        x=85+405*i/max(1,len(values)-1);y=30+125*float(amount(value['total']))/peak
        points.extend([x,y])
    if len(points)>2: d.add(PolyLine(points,strokeColor=TEAL,strokeWidth=2))
    elif points: d.add(Rect(points[0]-2,points[1]-2,4,4,fillColor=TEAL,strokeColor=None))
    for i in sorted({0,len(values)//2,len(values)-1}):
        if i>=0:
            d.add(String(85+405*i/max(1,len(values)-1),12,values[i]['date'][:7] if monthly else values[i]['date'][5:],textAnchor='middle',fontName='Balans',fontSize=8,fillColor=MUTED))
    return d


def render_pdf(snapshot):
    if snapshot.get('currency_reports'):
        from pypdf import PdfWriter
        writer=PdfWriter()
        for child in snapshot['currency_reports']:writer.append(BytesIO(render_pdf(child)))
        out=BytesIO();writer.write(out);return out.getvalue()
    currency=snapshot.get('currency','RUB')
    register_fonts();out=BytesIO()
    normal=ParagraphStyle('normal',fontName='Balans',fontSize=9,leading=14,textColor=INK,spaceAfter=8)
    small=ParagraphStyle('small',parent=normal,fontSize=8,leading=11)
    heading=ParagraphStyle('heading',parent=normal,fontName='BalansBold',fontSize=15,leading=20,spaceBefore=16,spaceAfter=12)
    title=ParagraphStyle('title',parent=heading,fontSize=28,leading=34,spaceBefore=0)
    def p(text,style=normal):return Paragraph(escape(str(text)).replace('\n','<br/>'),style)
    summary=snapshot['summary'];previous=snapshot['previous'];story=[]
    story += [p('Баланс / Финансовый отчёт',title),p(f"{snapshot['start']} - {snapshot['end']} | {currency} | {snapshot['timezone']}"),
              p(snapshot.get('workspace','Личный бюджет') ,small),Spacer(1,12),p(fmt(summary['total'])+' '+currency,title),
              p(f"Операций: {summary['count']}   |   В среднем за день: {fmt(summary['daily_average'])} {currency}")]
    if snapshot.get('filter_member'):story += [p('Выборка по участнику: '+str(snapshot['filter_member']),small)]
    story += [p(f"Доходы: {fmt(summary.get('income','0'))} {currency} | Возвраты: {fmt(summary.get('refunds','0'))} {currency} | Чистые расходы: {fmt(summary.get('net_expenses',summary['total']))} {currency}",small)]
    if not summary['count']:
        story += [p('Нет расходов за выбранный период.' if summary.get('operation_count') else 'Нет операций за выбранный период.',heading)]
    else:
        story += [p('Структура расходов',heading),category_chart(summary['categories']),p(('Динамика по месяцам' if len(summary['days'])<=62 else 'Динамика по 7-месячным интервалам') if summary.get('granularity')=='month' else ('Динамика по дням' if len(summary['days'])<=62 else 'Динамика по 7-дневным интервалам'),heading),daily_chart(summary['days'],summary.get('granularity')=='month')]
    story += [PageBreak(),p('Сравнение и крупнейшие расходы',title),
              p(f"Период сравнения: {snapshot['previous_start']} - {snapshot['previous_end']}."),
              p(f"Расходы периода сравнения: {fmt(previous['total'])} {currency}. Изменение: {fmt(snapshot['delta'])} {currency}."),
              p('Процент изменения: '+(snapshot['delta_percent']+'%' if snapshot['delta_percent'] is not None else 'не вычисляется: в периоде сравнения нет расходов.')),
              p('Графики показывают расходы до возвратов. Начальный остаток не считается доходом. Наличие записей не означает полноту учёта.',small)]
    top=sorted([r for r in snapshot['rows'] if r.get('kind','expense')=='expense'],key=lambda r:amount(r['amount']),reverse=True)[:20]
    if top:
        story += [p('До 20 крупнейших операций',heading)]
        data=[[p('Дата',small),p('Покупка / категория',small),p('Сумма '+currency,small)]]
        data += [[p(r['date'],small),p(r['description'][:180]+'\n'+r['category'],small),p(fmt(r['amount']),small)] for r in top]
        table=Table(data,colWidths=[77,308,110],repeatRows=1,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E6F3F4')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#DCE5ED'))]))
        story += [table]
    if snapshot.get('funds'):
        funds=snapshot['funds']
        story += [PageBreak(),p('Совместный бюджет',title),p(snapshot.get('workspace','')),p('Деньги в пути на конец периода: '+fmt(funds['transit'])+' '+currency,heading)]
        for member in funds['members']:
            story += [p('Участник '+member['participant'],heading),p(f"На начало: {fmt(member['opening'])} {currency} | На конец: {fmt(member['closing'])} {currency} | Заявленный остаток: {fmt(member['declared'])} {currency}"),p(f"Получено: {fmt(member['received'])} {currency}; возвращено руководителю: {fmt(member['returned'])} {currency}; в пути к участнику: {fmt(member['transit'])} {currency}; несверенные приходы: {fmt(member['pending'])} {currency}. Расходы периода без принятия: {fmt(member['unreviewed'])} {currency}.",small)]
    story += [Spacer(1,14),p('Отчёт является снимком учётных данных на '+snapshot['created_at']+'. Полный список доступен в CSV. AI-анализ вызывается отдельно в боте.',small)]
    def footer(canvas,doc):
        canvas.setFont('Balans',8);canvas.setFillColor(MUTED)
        canvas.drawString(50,28,'Баланс | Финансовый отчёт');canvas.drawRightString(A4[0]-50,28,str(doc.page))
    SimpleDocTemplate(out,pagesize=A4,rightMargin=50,leftMargin=50,topMargin=38,bottomMargin=48,title='Баланс - отчёт о расходах',author='Баланс').build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()
