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


def render_pdf(snapshot,include_operations=True):
    if snapshot.get('currency_reports'):
        from pypdf import PdfWriter
        writer=PdfWriter()
        for child in snapshot['currency_reports']:writer.append(BytesIO(render_pdf(child,include_operations=include_operations)))
        out=BytesIO();writer.write(out);return out.getvalue()
    currency=snapshot.get('currency','RUB')
    register_fonts();out=BytesIO()
    normal=ParagraphStyle('normal',fontName='Balans',fontSize=9,leading=14,textColor=INK,spaceAfter=8)
    small=ParagraphStyle('small',parent=normal,fontSize=8,leading=11)
    heading=ParagraphStyle('heading',parent=normal,fontName='BalansBold',fontSize=15,leading=20,spaceBefore=16,spaceAfter=12)
    title=ParagraphStyle('title',parent=heading,fontSize=28,leading=34,spaceBefore=0)
    def p(text,style=normal):return Paragraph(escape(str(text)).replace('\n','<br/>'),style)
    summary=snapshot['summary'];previous=snapshot['previous'];story=[]
    from datetime import date
    def day(value):return date.fromisoformat(value).strftime('%d.%m.%Y')
    symbol={'RUB':'₽','USD':'$','EUR':'€'}.get(currency,currency)
    def cash(value):return fmt(value)+' '+symbol
    story += [p('Баланс',title),p('Финансовый отчёт',heading),p(day(snapshot['start'])+' — '+day(snapshot['end'])+' · '+currency),Spacer(1,10)]
    story += [p('Доходы: '+cash(summary.get('income','0')),heading),p('Расходы: '+cash(summary['total']),heading)]
    if amount(summary.get('refunds','0')):story.append(p('Возвраты: '+cash(summary['refunds'])))
    flow=amount(summary.get('income','0'))-amount(summary['total'])+amount(summary.get('refunds','0'))
    story.append(p('Разница за период: '+cash(flow)))
    opening=sum((amount(r['amount']) for r in snapshot['rows'] if r.get('kind')=='opening'),amount(0))
    if opening:story.append(p('Начальный остаток: '+cash(opening)+' (не входит в доходы)',small))
    if summary['categories']:
        story += [p('На что потратили',heading),category_chart(summary['categories'])]
        story += [p(c['name']+' — '+cash(c['total'])+' · '+str(c['share'])+'%',small) for c in summary['categories']]
    else:story.append(p('За этот период расходов нет.'))
    if include_operations:
        story += [PageBreak(),p('Все операции за период',heading)]
        rows=sorted(snapshot['rows'],key=lambda r:(r['date'],r.get('id','')))
        if rows:
            data=[[p('Дата',small),p('Операция / категория',small),p('Сумма',small)]]
            kinds={'expense':'Расход','income':'Доход','opening':'Начальный остаток','refund':'Возврат','transfer':'Перевод'}
            for r in rows:
                kind=r.get('kind','expense')
                prefix='−' if kind=='expense' else '+' if kind in ('income','refund') else ''
                detail=kinds.get(kind,kind)+'\n'+r['description']
                if kind=='expense':detail+='\n'+r['category']
                data.append([p(day(r['date']),small),p(detail,small),p(prefix+cash(r['amount']),small)])
            table=Table(data,colWidths=[77,308,110],repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E6F3F4')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#DCE5ED'))]))
            story.append(table)
        else:story.append(p('За этот период записей нет.'))
    if snapshot.get('funds'):
        funds=snapshot['funds']
        story += [PageBreak(),p('Совместный бюджет',title),p(snapshot.get('workspace','')),p('Деньги в пути на конец периода: '+fmt(funds['transit'])+' '+currency,heading)]
        for member in funds['members']:
            story += [p('Участник '+member['participant'],heading),p(f"На начало: {fmt(member['opening'])} {currency} | На конец: {fmt(member['closing'])} {currency} | Заявленный остаток: {fmt(member['declared'])} {currency}"),p(f"Получено: {fmt(member['received'])} {currency}; возвращено руководителю: {fmt(member['returned'])} {currency}; в пути к участнику: {fmt(member['transit'])} {currency}; несверенные приходы: {fmt(member['pending'])} {currency}. Расходы периода без принятия: {fmt(member['unreviewed'])} {currency}.",small)]
    story += [Spacer(1,14),p('Отчёт является снимком учётных данных на '+snapshot['created_at']+'.',small)]
    def footer(canvas,doc):
        canvas.setFont('Balans',8);canvas.setFillColor(MUTED)
        canvas.drawString(50,28,'Баланс | Финансовый отчёт');canvas.drawRightString(A4[0]-50,28,str(doc.page))
    SimpleDocTemplate(out,pagesize=A4,rightMargin=50,leftMargin=50,topMargin=38,bottomMargin=48,title='Баланс - отчёт о расходах',author='Баланс').build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()
