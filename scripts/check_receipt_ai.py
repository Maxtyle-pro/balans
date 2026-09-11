"""Live OCR smoke test on synthetic receipts; never reads user history."""
import argparse
from io import BytesIO
from pathlib import Path
import sys
from uuid import NAMESPACE_URL,uuid5

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from PIL import Image,ImageDraw,ImageFont
from balans.ai import CategoryAI
from balans.receipt_ai import ReceiptAI
from balans.receipt_media import prepare


def fixtures(root):
    font_path=next((p for p in [Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()),None)
    if font_path is None:
        raise SystemExit('Для синтетического русского чека нужен шрифт Arial или DejaVuSans.')
    image=Image.new('RGB',(1000,1200),'white');draw=ImageDraw.Draw(image)
    font=ImageFont.truetype(str(font_path),40)
    text='ТЕСТОВЫЙ МАГАЗИН\nКАССОВЫЙ ЧЕК\n10.09.2026\n\nМолоко       100,00\nХлеб            50,00\n\nИТОГ          150,00 РУБ\nНАЛИЧНЫМИ 200,00\nСДАЧА           50,00\nОПЛАЧЕНО\n\nСинтетический пример'
    draw.multiline_text((60,45),text,font=font,fill='black',spacing=22)
    image.save(root/'photo.png');image.save(root/'scan.pdf','PDF');image.close()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from receipt_fixtures import pdf_bytes
    (root/'text.pdf').write_bytes(pdf_bytes())
    for name in ('text.pdf','scan.pdf'):
        rendered=prepare((root/name).read_bytes())
        (root/(name+'.jpg')).write_bytes(rendered.images[0])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-only',action='store_true',help='Создать только синтетические тестовые файлы')
    args=parser.parse_args()
    root=Path('.local/receipt-smoke');root.mkdir(parents=True,exist_ok=True)
    fixtures(root)
    if args.prepare_only:
        print('Синтетические фото, текстовый PDF и PDF-скан подготовлены в .local/receipt-smoke.');return
    load_dotenv()
    category_ai=CategoryAI.from_env()
    if not category_ai.available:
        raise SystemExit('OPENAI_API_KEY не настроен.')
    ai=ReceiptAI(category_ai.client,category_ai.model)
    categories=[{'id':str(uuid5(NAMESPACE_URL,'balans/category/'+n)),'name':n} for n in ['Продукты','Кафе и рестораны','Транспорт','Дом','Здоровье','Покупки','Развлечения','Другое']]
    failures=0
    try:
        for name in ('photo.png','text.pdf','scan.pdf'):
            result=ai.extract(prepare((root/name).read_bytes()),categories)
            payload=result.result or {}
            total=payload.get('total')
            from decimal import Decimal
            passed=not result.error_code and total is not None and Decimal(total)==Decimal('150') and payload.get('document_kind')=='expense' and payload.get('currency')=='RUB' and len(payload.get('items',[]))==2
            failures+=not passed
            print(f"{'OK' if passed else 'FAIL'} {name}: итог={total}, позиций={len(payload.get('items',[]))}, код={result.error_code or 'ok'}",flush=True)
        if failures:
            raise SystemExit(1)
    finally:
        category_ai.close()


if __name__=='__main__':
    main()
