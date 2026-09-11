"""Two live API checks using generated synthetic financial screens only."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from uuid import uuid5,NAMESPACE_URL
from dotenv import load_dotenv
from PIL import Image,ImageDraw,ImageFont
from balans.ai import CategoryAI
from balans.receipt_ai import ReceiptAI
from balans.receipt_media import prepare


def picture(text):
    im=Image.new('RGB',(1200,1200),'#f5f7fb');d=ImageDraw.Draw(im)
    font=ImageFont.truetype(str(Path(__file__).resolve().parent.parent/'balans/fonts/DejaVuSans.ttf'),38)
    d.multiline_text((45,40),text,font=font,fill='#102039',spacing=24)
    b=BytesIO();im.save(b,'PNG');return b.getvalue()


def main():
    load_dotenv();category_ai=CategoryAI.from_env()
    if not category_ai.available:raise SystemExit('AI не настроен.')
    ai=ReceiptAI(category_ai.client,category_ai.model)
    categories=[{'id':str(uuid5(NAMESPACE_URL,'media-test/'+n)),'name':n} for n in ('Продукты','Кафе и рестораны','Транспорт','Другое')]
    samples=[('history','История операций\nКарта **** 1234\nБаланс: 50 000 RUB\n\n10.09.2026\nКафе: оплата 600 RUB\nСтатус: выполнено\nID операции TX600\n\n10.09.2026\nМетро: оплата 70 RUB\nСтатус: в обработке\nID операции TX070\n\nСинтетический пример'),('terminal','Касса самообслуживания\nМагазин ТЕСТ\n\nМолоко     100 RUB\nХлеб            50 RUB\n\nК ОПЛАТЕ: 150 RUB\n\nВставьте карту или приложите телефон\nОплата ещё не выполнена\n\nСинтетический пример')]
    def check(sample):
        name,caption=sample;result=ai.extract(prepare(picture(caption)),categories);data=result.result or {};rows=data.get('transactions',[])
        if name=='history':ok=False
        else:ok=len(rows)==1 and rows[0]['amount'] in ('150','150.00') and rows[0]['payment_status'] in ('unknown','pending')
        # Amount formatting may contain decimal zeros.
        if name=='history' and len(rows)==2 and all(r['amount'] for r in rows):
            from decimal import Decimal
            ok=sorted((Decimal(r['amount']),r['payment_status']) for r in rows)==[(Decimal(70),'pending'),(Decimal(600),'paid')]
        print(name, 'OK' if ok else 'FAIL', 'source='+str(data.get('source_type')),'total='+str(data.get('total')), 'rows='+str([(r['amount'],r['kind'],r['payment_status']) for r in rows]),'error='+str(result.error_code),flush=True)
        return ok
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(check,samples))
        if not all(results):raise SystemExit(1)
    finally:category_ai.close()

if __name__=='__main__':main()
