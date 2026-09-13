import base64
from io import BytesIO
from pypdf import PdfReader
from test_privacy import private_service
from test_documents import op,attach
from test_service import send,query
from test_receipts import button
from balans.detailed_report import render_detailed
from scripts.check_report_pdf import fixture


def test_detailed_export_scope_and_image(private_service,database):
    s=private_service;u=394001;other=394002
    operation=op(s,u,database);attach(s,u,operation)
    card=send(s,u,'/report')
    callback=button(card,'🧾 Детализированный отчёт')
    assert 'недоступен' in send(s,other,callback=callback).text
    report=send(s,u,callback=callback)
    assert report.generated_filename.endswith('.pdf') and not report.text
    pdf=PdfReader(BytesIO(base64.b64decode(report.generated_document)))
    assert 'Детализированный отчёт' in ''.join(p.extract_text() for p in pdf.pages)
    assert any(len(p.images)>0 for p in pdf.pages)
    doc=query(database,u,'SELECT id FROM documents')[0][0]
    s.document_storage.personal.remove(doc)
    report=send(s,u,callback=callback)
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(report.generated_document))).pages)
    assert 'недоступна' in text


def test_pdf_appendix_and_shared_image_once():
    from receipt_fixtures import photo_bytes,pdf_bytes
    snap=fixture();snap['rows']=snap['rows'][:2]
    sources={r['id']:{'label':'Фото / изображение','text':'Проверочный текст','files':['photo','pdf'],'missing':False} for r in snap['rows']}
    files={'photo':{'mime':'image/png','data':photo_bytes(),'number':1},'pdf':{'mime':'application/pdf','data':pdf_bytes(pages=2),'number':2}}
    pdf=PdfReader(BytesIO(render_detailed(snap,sources,files)))
    text=''.join(p.extract_text() for p in pdf.pages)
    assert 'Приложение' in text and 'Проверочный текст' in text
    assert 'К операции 01' in text and 'К операции 02' in text
    assert sum(len(p.images) for p in pdf.pages)==1
    page_ids={page.indirect_reference.idnum for page in pdf.pages}
    links=[ref.get_object() for page in pdf.pages for ref in page.get('/Annots',[])]
    assert len(links)==10  # four forward links, two return links on each appendix page
    assert all(a['/Dest'][0].idnum in page_ids for a in links)
    operation_page=next(page for page in pdf.pages if 'Проверочный текст' in page.extract_text())
    assert len(operation_page.images)==0


def test_original_text_in_detailed_report(service):
    from test_money_recognition import TextFake,enable
    s=service;u=394003;enable(s,u);s.text_ai=TextFake('income')
    send(s,u,'Получил гонорар за проект')
    card=send(s,u,'/report')
    result=send(s,u,callback=button(card,'🧾 Детализированный отчёт'))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(result.generated_document))).pages)
    assert 'Получил гонорар за проект' in text
    assert 'Текстовое сообщение' in text


from test_voice import voices

def test_voice_transcript_in_detailed_report(voices):
    from test_voice import upload
    from test_money_recognition import enable
    s,_=voices;u=394004;enable(s,u)
    upload(s,u)
    card=send(s,u,'/report all')
    result=send(s,u,callback=button(card,'🧾 Детализированный отчёт'))
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(result.generated_document))).pages)
    assert 'Вчера потратил 850 рублей на продукты' in text
    assert 'Голосовое сообщение' in text
