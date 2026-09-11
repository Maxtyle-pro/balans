"""Synthetic fixtures only; no real people's receipts."""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject


def photo_bytes(rotated=False):
    image=Image.new('RGB',(900,1100),'white')
    draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=36)
    draw.multiline_text((60,50),'TEST SHOP\nRECEIPT\n2026-09-10\n\nMilk       100.00\nBread       50.00\n\nTOTAL RUB  150.00\nCASH       200.00\nCHANGE      50.00\nPAID',font=font,fill='black',spacing=20)
    if rotated:
        image=image.rotate(90,expand=True)
    out=BytesIO();image.save(out,'PNG');image.close();return out.getvalue()


def pdf_bytes(pages=1,encrypted=False):
    writer=PdfWriter()
    for _ in range(pages):
        page=writer.add_blank_page(width=400,height=600)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 18 Tf 30 560 Td (TEST SHOP) Tj 0 -30 Td (2026-09-10) Tj 0 -30 Td (Milk 100.00) Tj 0 -30 Td (Bread 50.00) Tj 0 -30 Td (TOTAL RUB 150.00) Tj 0 -30 Td (CASH 200.00 CHANGE 50.00) Tj 0 -30 Td (PAID) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
    if encrypted:
        writer.encrypt('test-password')
    out=BytesIO();writer.write(out);return out.getvalue()


def scan_pdf_bytes():
    with Image.open(BytesIO(photo_bytes())) as image:
        out=BytesIO();image.save(out,'PDF');return out.getvalue()


def receipt_payload(category_id=None,**changes):
    payload={'document_kind':'expense','merchant':'TEST SHOP','occurred_on':'2026-09-10','total':'150.00','currency':'RUB',
             'category_id':category_id,'payment_status':'paid','confidence_total':0.98,
             'items':[{'name':'Milk','quantity':'1','unit_price':'100.00','line_total':'100.00','discount':None},
                      {'name':'Bread','quantity':'1','unit_price':'50.00','line_total':'50.00','discount':None}],
             'items_complete':True,'ocr_text':'TEST SHOP TOTAL 150.00 RUB PAID'}
    payload.update(changes)
    return payload
