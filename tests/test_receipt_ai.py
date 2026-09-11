import json
from uuid import uuid4
import httpx
from openai import OpenAI
import pytest

from balans.receipt_ai import ReceiptAI,ReceiptResult,validate_receipt
from balans.receipt_media import Prepared
from receipt_fixtures import receipt_payload

ID=str(uuid4());CATS=[{'id':ID,'name':'Продукты'}]


def test_total_not_cash_and_warning_not_double_count():
    r=validate_receipt(ReceiptResult(**receipt_payload(ID)),CATS)
    assert r['total']=='150.00'
    assert not r['warnings']
    r=validate_receipt(ReceiptResult(**receipt_payload(ID,total='200.00')),CATS)
    assert 'не совпадает' in r['warnings'][0]


@pytest.mark.parametrize('changes',[{'total':'NaN'},{'total':'-100'},{'total':'1.001'},{'total':'1e9'},{'total':'1000000000000.00'}])
def test_invalid_money(changes):
    with pytest.raises(ValueError):
        validate_receipt(ReceiptResult(**receipt_payload(ID,**changes)),CATS)


def test_low_confidence_date_and_foreign_category():
    r=validate_receipt(ReceiptResult(**receipt_payload(str(uuid4()),occurred_on='2026-02-31',confidence_total=0.2,ocr_text='card 1234 5678 9012 3456')),CATS)
    assert r['total'] is None and r['occurred_on'] is None and r['category_id'] is None
    assert '1234' not in r['ocr_text']


def test_sdk_multimodal_request():
    def handler(request):
        body=json.loads(request.content)
        assert body['store'] is False
        assert body['text']['format']['strict'] is True
        content=body['input'][1]['content']
        assert content[1]['type']=='input_image'
        assert content[1]['image_url'].startswith('data:image/jpeg;base64,')
        assert content[1]['detail']=='high'
        assert set(json.loads(content[0]['text']))=={'categories','pdf_text'}
        return httpx.Response(200,json={'id':'resp_receipt','object':'response','created_at':1789000000,'status':'completed','model':'test',
          'output':[{'type':'message','id':'msg','status':'completed','role':'assistant','content':[{'type':'output_text','text':json.dumps(receipt_payload(ID)),'annotations':[]}]}],
          'usage':{'input_tokens':500,'output_tokens':300,'total_tokens':800}})
    with OpenAI(api_key='test',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        result=ReceiptAI(client).extract(Prepared('image/png',1,'',[b'test-image']),CATS)
        assert result.error_code is None
        assert result.result['total']=='150.00'
        assert result.input_tokens==500
