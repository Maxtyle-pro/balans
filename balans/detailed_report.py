"""Private source lookup and bounded PDF export of operation evidence."""
from io import BytesIO
from uuid import UUID
from datetime import date
from xml.sax.saxutils import escape
from PIL import Image as PILImage,ImageOps
from pypdf import PdfReader,PdfWriter
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Image,PageBreak,KeepTogether
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.colors import HexColor
from balans.report_pdf import render_pdf,register_fonts
from balans.report_data import fmt
from balans.receipt_media import MediaError

LIMIT=30*1024*1024


def collect_sources(service,c,report):
    sources={};files={};total=0
    for row in report['snapshot']['rows']:
        op=c.execute('SELECT o.source_draft_id,d.receipt_batch_id,d.voice_job_id FROM operations o LEFT JOIN operation_drafts d ON d.id=o.source_draft_id WHERE o.id=%s AND o.workspace_id=%s',(UUID(row['id']),report['workspace_id'])).fetchone()
        info={'label':{'voice':'Голосовое сообщение','receipt':'Фото или документ','manual':'Текст / ручной ввод','text':'Текстовое сообщение'}.get(row.get('source'),'Источник не сохранён'),'text':None,'files':[],'missing':False}
        sources[row['id']]=info
        if not op:continue
        if op['voice_job_id']:
            voice=c.execute('SELECT transcript FROM voice_jobs WHERE id=%s',(op['voice_job_id'],)).fetchone()
            info['label']='Голосовое сообщение';info['text']=voice['transcript'] if voice else None
        elif op['source_draft_id']:
            job=c.execute("SELECT j.request->>'message' AS message FROM input_batches b JOIN text_jobs j ON j.reply->>'capture_batch_id'=b.id::text WHERE b.workspace_id=%s AND EXISTS(SELECT 1 FROM jsonb_array_elements(b.items) item WHERE item->>'draft_id'=%s) LIMIT 1",(report['workspace_id'],str(op['source_draft_id']))).fetchone()
            if job:info['label']='Текстовое сообщение';info['text']=job['message']
        attachments=c.execute("SELECT id,mime_type,sha256,permanent,source_receipt_id,state='active' AND (expires_at IS NULL OR expires_at>now()) AS available FROM documents WHERE set_id=%s AND workspace_id=%s ORDER BY created_at,id",(UUID(row['id']),report['workspace_id'])).fetchall()
        candidates=[(a,service.document_storage.shared if a['permanent'] else service.document_storage.personal) for a in attachments]
        if op['receipt_batch_id']:
            receipt=c.execute("SELECT id,mime_type,sha256,expires_at>now() AND telegram_file_id<>'' AS available FROM receipt_files WHERE batch_id=%s AND workspace_id=%s ORDER BY created_at,id",(op['receipt_batch_id'],report['workspace_id'])).fetchall()
            represented={a['source_receipt_id'] for a in attachments}
            candidates += [(a,service.receipt_storage) for a in receipt if a['id'] not in represented]
            info['label']='Документ PDF' if any(a['mime_type']=='application/pdf' for a,_ in candidates) else 'Фото / изображение'
        for a,storage in candidates:
            if not a['available']:info['missing']=True;continue
            key=a['sha256']
            if key not in files:
                try:raw=storage.read(a['id'])
                except (MediaError,FileNotFoundError):info['missing']=True;continue
                total+=len(raw)
                if total>LIMIT:raise ValueError('Вложения слишком большие для одного отчёта. Выберите более короткий период.')
                files[key]={'data':raw,'mime':a['mime_type'],'number':len(files)+1}
            if key not in info['files']:info['files'].append(key)
    return sources,files


def render_detailed(snapshot,sources,files):
    register_fonts()
    normal=ParagraphStyle('detail',fontName='Balans',fontSize=10,leading=15,textColor=HexColor('#172D40'),spaceAfter=8)
    heading=ParagraphStyle('detailTitle',parent=normal,fontName='BalansBold',fontSize=18,leading=23,spaceBefore=12,spaceAfter=12)
    small=ParagraphStyle('detailSmall',parent=normal,fontSize=9,leading=13,textColor=HexColor('#607383'))
    def p(s,style=normal):return Paragraph(escape(str(s)).replace('\n','<br/>'),style)
    story=[p('Детализированный отчёт',heading),p(snapshot['start']+' — '+snapshot['end'],small)]
    shown=set();appendices=[]
    for i,row in enumerate(snapshot['rows'],1):
        operation_start=len(story)
        info=sources[row['id']];kind=row.get('kind','expense');symbol='₽' if row.get('currency','RUB')=='RUB' else row['currency']
        sign='−' if kind=='expense' else '+' if kind in ('income','refund') else ''
        title=f"{i:02d} · {row['description']}"
        subtitle=f"{date.fromisoformat(row['date']):%d.%m.%Y} · {sign}{fmt(row['amount'])} {symbol}"
        if kind=='expense':subtitle+=' · '+row['category']
        else:subtitle+=' · '+{'income':'Доход','opening':'Начальный остаток','refund':'Возврат'}.get(kind,'Операция')
        story.extend([Spacer(1,12),p(title,heading),p(subtitle),p('Источник: '+info['label'],small)])
        if int(row.get('revision',1))>1:story.append(p('Запись изменена пользователем. Показаны данные на момент формирования отчёта.',small))
        if info['text']:story += [p('Распознанная речь' if info['label']=='Голосовое сообщение' else 'Сохранённый текст сообщения',small),p(info['text'])]
        elif not info['files']:story.append(p('Исходное сообщение не сохранилось.',small))
        for key in info['files']:
            f=files[key];name='Вложение '+str(f['number'])
            if key in shown:story.append(p(name+' — показано у предыдущей связанной операции.',small));continue
            shown.add(key)
            if f['mime']=='application/pdf':
                try:
                    doc=PdfReader(BytesIO(f['data']))
                    if doc.is_encrypted:raise ValueError('encrypted')
                    if sum(len(x[1].pages) for x in appendices)+len(doc.pages)>150:raise ValueError('too many pages')
                    appendices.append((name,doc));story.append(p(name+' · PDF, страниц: '+str(len(doc.pages))+'. Полный документ — в приложении в конце отчёта.',small))
                except Exception:story.append(p(name+' · Не удалось включить PDF. Оригинал можно скачать в разделе «Мои файлы».',small))
            else:
                try:
                    with PILImage.open(BytesIO(f['data'])) as source:
                        picture=ImageOps.exif_transpose(source).convert('RGB');picture.thumbnail((1400,1800));buf=BytesIO();picture.save(buf,'JPEG',quality=85);buf.seek(0)
                        scale=min(480/picture.width,450/picture.height,1)
                        story += [p(name+' · изображение',small),Image(buf,width=picture.width*scale,height=picture.height*scale)]
                except Exception:story.append(p(name+' · Не удалось показать изображение.',small))
        if info['missing']:story.append(p('Часть вложений больше не хранится или недоступна.',small))
        story[operation_start:]=[KeepTogether(story[operation_start:])]
    if not snapshot['rows']:story.append(p('За этот период записей нет.'))
    out=BytesIO()
    SimpleDocTemplate(out,rightMargin=45,leftMargin=45,topMargin=40,bottomMargin=45).build(story)
    writer=PdfWriter();writer.append(BytesIO(render_pdf(snapshot,include_operations=False)));writer.append(BytesIO(out.getvalue()))
    for name,doc in appendices:
        cover=BytesIO();SimpleDocTemplate(cover).build([p('Приложение · '+name,heading),p('Исходный документ. Следующие страницы содержат оригинал.')]);writer.append(BytesIO(cover.getvalue()))
        writer.add_outline_item(name,len(writer.pages)-1)
        for page in doc.pages:writer.add_page(page,excluded_keys=['/Annots','/AA'])
    final=BytesIO();writer.write(final)
    if final.tell()>45*1024*1024:raise ValueError('PDF слишком большой. Выберите более короткий период.')
    return final.getvalue()
