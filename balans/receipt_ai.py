from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from openai import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from balans.ai import DEFAULT_MODEL

RECEIPT_PROMPT=Path(__file__).with_name('prompts').joinpath('receipt_v2.txt').read_text()


class Line(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: str=Field(min_length=1,max_length=160)
    quantity: str | None
    unit_price: str | None
    line_total: str | None
    discount: str | None
    category_id: str | None = None


class ImageTransaction(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind: Literal['expense','income','opening','incoming','refund','transfer','unknown']
    amount: str | None
    currency: str | None
    occurred_on: str | None
    merchant: str | None=Field(max_length=200)
    description: str=Field(max_length=500)
    category_id: str | None
    payment_status: Literal['paid','unknown','pending','failed']
    confidence_amount: float=Field(ge=0,le=1)
    account_hint: str | None=Field(max_length=80)
    transaction_reference: str | None=Field(max_length=200)
    items: list[Line]=Field(max_length=100)
    items_complete: bool=False


class ReceiptResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    receipt_reference: str | None=Field(default=None,max_length=200)
    source_type: Literal['receipt','screenshot','terminal','unknown']='receipt'
    transactions: list[ImageTransaction]=Field(default_factory=list,max_length=8)
    document_kind: Literal['expense','refund','other','multiple']
    merchant: str | None=Field(max_length=200)
    occurred_on: str | None
    total: str | None
    currency: str | None
    category_id: str | None
    payment_status: Literal['paid','unknown','pending','failed']
    confidence_total: float=Field(ge=0,le=1)
    items: list[Line]=Field(max_length=100)
    items_complete: bool
    ocr_text: str=Field(max_length=12000)


@dataclass
class ReceiptExtraction:
    result: dict | None=None
    error_code: str | None=None
    response_id: str | None=None
    input_tokens: int | None=None
    output_tokens: int | None=None


def mask(text):
    # Never expose full long card/account/phone identifiers in text previews.
    return re.sub(r'(?<!\w)\+?\d(?:[ -]?\d){10,}(?!\w)','[реквизиты скрыты]',text)


def number(value,quantity=False):
    if value is None:
        return None
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,'+('6' if quantity else '2')+r'})?',value):
        raise ValueError('invalid_number')
    result=Decimal(value)
    if quantity and result<=0:
        raise ValueError('invalid_quantity')
    return result


def validate_receipt(result,categories):
    output=result.model_dump()
    reference=output.pop('receipt_reference')
    output['reference_hash']=hashlib.sha256(reference.strip().encode()).hexdigest() if reference and reference.strip() else None
    output['warnings']=[]
    allowed={c['id'] for c in categories}
    if output['category_id'] not in allowed:
        output['category_id']=None
    if output['currency'] is not None:
        if not re.fullmatch('[A-Z]{3}',output['currency']):
            output['currency']=None
    amount=number(output['total'])
    if amount is not None and (amount<=0 or result.confidence_total<0.75):
        output['total']=None
        output['warnings'].append('Итог не прочитан уверенно — введите сумму вручную.')
    if output['occurred_on']:
        try:
            datetime.strptime(output['occurred_on'],'%Y-%m-%d')
        except ValueError:
            output['occurred_on']=None
    totals=[]
    for line in output['items']:
        line['name']=mask(line['name'])
        if line.get('category_id') not in allowed:line['category_id']=None
        for key in ('quantity','unit_price','line_total','discount'):
            number(line[key],quantity=key=='quantity')
        if line['line_total'] is not None:
            totals.append(Decimal(line['line_total']))
    if result.items_complete and len(totals)==len(result.items) and totals and amount is not None:
        if abs(sum(totals)-amount)>Decimal('0.01'):
            output['warnings'].append('Сумма позиций не совпадает с итогом. Проверьте скидки и итог перед сохранением.')
    if not result.items_complete:
        output['warnings'].append('Перечень позиций может быть неполным.')
    for item in output['transactions']:
        amount=number(item['amount'])
        if amount is not None and (amount<=0 or item['confidence_amount']<0.75):item['amount']=None
        if item['currency'] is not None and not re.fullmatch('[A-Z]{3}',item['currency']):item['currency']=None
        if item['occurred_on']:
            try:datetime.strptime(item['occurred_on'],'%Y-%m-%d')
            except ValueError:item['occurred_on']=None
        if item['category_id'] not in allowed:item['category_id']=None
        for key in ('merchant','description','account_hint'):item[key]=mask(item[key]) if item[key] else item[key]
        ref=item.pop('transaction_reference')
        item['reference_hash']=hashlib.sha256(ref.strip().encode()).hexdigest() if ref and ref.strip() else None
        item['warnings']=[]
        if item['items'] and not item['items_complete']:item['warnings'].append('Перечень позиций может быть неполным.')
        totals=[]
        for line in item['items']:
            line['name']=mask(line['name'])
            if line.get('category_id') not in allowed:line['category_id']=None
            for key in ('quantity','unit_price','line_total','discount'):number(line[key],quantity=key=='quantity')
            if line['line_total'] is not None:totals.append(Decimal(line['line_total']))
        if item['items_complete'] and totals and len(totals)==len(item['items']) and amount is not None and abs(sum(totals)-amount)>Decimal('.01'):item['warnings'].append('Сумма позиций не совпадает с итогом. Проверьте сумму и скидки.')
    output['ocr_text']=mask(output['ocr_text'])
    output['merchant']=mask(output['merchant']) if output['merchant'] else None
    if output['source_type'] in ('screenshot','terminal','unknown') and not output['transactions'] and output['document_kind']!='multiple':
        output['transactions']=[{'kind':{'expense':'expense','refund':'refund'}.get(output['document_kind'],'unknown'),'amount':output['total'],'currency':output['currency'],'occurred_on':output['occurred_on'],'merchant':output['merchant'],'description':(output['merchant'] or 'Операция по изображению'),'category_id':output['category_id'],'payment_status':output['payment_status'],'confidence_amount':output['confidence_total'],'account_hint':None,'reference_hash':output['reference_hash'],'items':output['items'],'items_complete':output['items_complete'],'warnings':output['warnings']}]
    return output


class ReceiptAI:
    def __init__(self,client=None,model=DEFAULT_MODEL):
        self.client=client
        self.model=model

    @property
    def available(self):
        return self.client is not None

    def extract(self,prepared,categories):
        if not self.available:
            return ReceiptExtraction(error_code='not_configured')
        content=[{'type':'input_text','text':json.dumps({'categories':categories,'pdf_text':prepared.text},ensure_ascii=False)}]
        content += [{'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(image).decode(),'detail':'high'} for image in prepared.images]
        try:
            response=self.client.with_options(timeout=45,max_retries=0).responses.parse(
                model=self.model,store=False,max_output_tokens=7000,
                input=[{'role':'system','content':RECEIPT_PROMPT},{'role':'user','content':content}],text_format=ReceiptResult)
            if response.status!='completed' or response.output_parsed is None:
                return ReceiptExtraction(error_code='incomplete_or_refused',response_id=response.id)
            parsed=ReceiptResult.model_validate(response.output_parsed)
            result=validate_receipt(parsed,categories)
            usage=response.usage
            return ReceiptExtraction(result,response_id=response.id,input_tokens=usage.input_tokens if usage else None,output_tokens=usage.output_tokens if usage else None)
        except (APIError,ValidationError,ValueError,TypeError,InvalidOperation) as exc:
            return ReceiptExtraction(error_code=type(exc).__name__)
