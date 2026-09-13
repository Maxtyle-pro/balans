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
    from reportlab.platypus import Table,TableStyle,Flowable
    from pypdf import Transformation
    register_fonts()
    normal=ParagraphStyle('detail',fontName='Balans',fontSize=10,leading=15,textColor=HexColor('#173E32'),spaceAfter=5)
    heading=ParagraphStyle('detailTitle',parent=normal,fontName='BalansBold',fontSize=18,leading=23,spaceAfter=12)
    title_style=ParagraphStyle('operationTitle',parent=normal,fontName='BalansBold',fontSize=12,leading=17)
    small=ParagraphStyle('detailSmall',parent=normal,fontSize=9,leading=13,textColor=HexColor('#61766D'))
    def p(s,style=normal):return Paragraph(escape(str(s)).replace('\n','<br/>'),style)
    def link(label,target):return Paragraph('<link href="#'+target+'" color="#207D55"><u>'+escape(label)+'</u></link>',normal)
    locations={}
    class Anchor(Flowable):
        def __init__(self,key):super().__init__();self.key=key
        def draw(self):
            self.canv.bookmarkHorizontal(self.key,0,0)
            locations[self.key]=(self.canv.getPageNumber()-1,self.canv.absolutePosition(0,0)[1])
    prepared={};pages=0
    for key,f in files.items():
        try:
            if f['mime']=='application/pdf':
                doc=PdfReader(BytesIO(f['data']))
                if doc.is_encrypted or pages+len(doc.pages)>150:raise ValueError('PDF limit')
                pages+=len(doc.pages);prepared[key]=('pdf',doc)
            else:
                with PILImage.open(BytesIO(f['data'])) as source:
                    picture=ImageOps.exif_transpose(source).convert('RGB');picture.thumbnail((1400,1800))
                    buf=BytesIO();picture.save(buf,'JPEG',quality=85);buf.seek(0)
                    prepared[key]=('image',(buf,picture.width,picture.height))
        except Exception:prepared[key]=('missing',None)
    story=[p('Детализированный отчёт',heading),p(snapshot['start']+' — '+snapshot['end'],small),p('Все операции',heading)]
    references={key:[] for key in files}
    for i,row in enumerate(snapshot['rows'],1):
        info=sources[row['id']];kind=row.get('kind','expense');symbol='₽' if row.get('currency','RUB')=='RUB' else row['currency']
        sign='−' if kind=='expense' else '+' if kind in ('income','refund') else ''
        cells=[p(f"{i:02d} · {row['description']}",title_style),p(f"{date.fromisoformat(row['date']):%d.%m.%Y} · {sign}{fmt(row['amount'])} {symbol} · "+(row['category'] if kind=='expense' else {'income':'Доход','opening':'Начальный остаток','refund':'Возврат'}.get(kind,'Операция')))]
        if int(row.get('revision',1))>1:cells.append(p('Запись изменена пользователем.',small))
        cells.append(p(info['label']+(' · расшифровка' if info['label']=='Голосовое сообщение' else ''),small))
        if info['text']:cells.append(p(info['text']))
        elif not info['files']:cells.append(p('Исходное сообщение не сохранилось.',small))
        for key in info['files']:
            number=files[key]['number'];references[key].append((i,row['description']))
            if prepared[key][0]=='missing':cells.append(p(f'Вложение {number} недоступно для просмотра в отчёте.',small))
            else:cells.append(link(f'Открыть '+('документ' if prepared[key][0]=='pdf' else 'фото')+f' → приложение {number}',f'file{number}'))
        if info['missing']:cells.append(p('Часть вложений больше не хранится или недоступна.',small))
        cells[0]=[Anchor(f'op{i}'),cells[0]]
        card=Table([[cell] for cell in cells],colWidths=[505],hAlign='LEFT',splitByRow=1,splitInRow=1)
        card.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),HexColor('#EFF8F0')),('LINEBEFORE',(0,0),(0,-1),3,HexColor('#8BC59D')),('LEFTPADDING',(0,0),(-1,-1),14),('RIGHTPADDING',(0,0),(-1,-1),14),('TOPPADDING',(0,0),(-1,0),12),('BOTTOMPADDING',(0,-1),(-1,-1),12)]))
        story.extend([KeepTogether([card]),Spacer(1,14)])
    if not snapshot['rows']:story.append(p('За этот период записей нет.'))
    overlays=[]
    for key,(kind,data) in prepared.items():
        if kind=='missing' or not references[key]:continue
        number=files[key]['number']
        for index in range(len(data.pages) if kind=='pdf' else 1):
            story.extend([PageBreak(),Anchor(f'file{number}' if index==0 else f'file{number}page{index}'),p(f'Приложение {number} · '+('Документ' if kind=='pdf' else 'Фото'),heading)])
            for i,description in references[key]:story.append(link(f'← К операции {i:02d} · {description}',f'op{i}'))
            if kind=='pdf':
                story.append(p(f'Страница {index+1} из {len(data.pages)}',small))
                marker=f'pdf{number}page{index}';story.append(KeepTogether([Anchor(marker),Spacer(1,490)]))
                overlays.append((marker,data.pages[index]))
            else:
                buf,w,h=data;scale=min(490/w,480/h,1)
                story.append(Image(buf,width=w*scale,height=h*scale))
    out=BytesIO()
    SimpleDocTemplate(out,rightMargin=45,leftMargin=45,topMargin=40,bottomMargin=45).build(story)
    detail=PdfWriter(clone_from=BytesIO(out.getvalue()))
    for marker,source in overlays:
        page_index,top=locations[marker]
        source.transfer_rotation_to_content()
        width=float(source.mediabox.width);height=float(source.mediabox.height)
        scale=min(495/width,480/height)
        for field in ('/Annots','/AA'):source.pop(field,None)
        transform=Transformation().translate(-float(source.mediabox.left),-float(source.mediabox.bottom)).scale(scale).translate(45+(495-width*scale)/2,top-480)
        detail.pages[page_index].merge_transformed_page(source,transform)
    writer=PdfWriter();writer.append(BytesIO(render_pdf(snapshot,include_operations=False)));detail_bytes=BytesIO();detail.write(detail_bytes);detail_bytes.seek(0);writer.append(detail_bytes)
    final=BytesIO();writer.write(final)
    if final.tell()>45*1024*1024:raise ValueError('PDF слишком большой. Выберите более короткий период.')
    return final.getvalue()
