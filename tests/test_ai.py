import json
from uuid import uuid4

import httpx
from openai import OpenAI
import pytest

from balans.ai import CategoryAI, CategoryResult, PROMPT, match_rule, normalize

CATEGORY = str(uuid4())
REQUEST = {'description':'Кофе в кофейне', 'categories':[{'id':CATEGORY, 'name':'Кафе и рестораны'}]}


def api_response(category=CATEGORY,confidence=0.95,status='completed',content=None):
    return {'id':'resp_test','object':'response','created_at':1789000000,'status':status,
            'model':'gpt-4.1-mini-2025-04-14','output':[{'type':'message','id':'msg_test','status':'completed','role':'assistant',
            'content':content or [{'type':'output_text','text':json.dumps({'category_id':category,'confidence':confidence}),'annotations':[]}]}],
            'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}


def adapter(handler):
    client=OpenAI(api_key='test-key',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return CategoryAI(client=client)


def test_real_sdk_serialization_and_validation():
    def handler(request):
        body=json.loads(request.content)
        assert body['store'] is False
        assert body['max_output_tokens']==160
        assert body['text']['format']['type']=='json_schema'
        assert body['text']['format']['strict'] is True
        assert body['input'][0]['content']==PROMPT
        assert json.loads(body['input'][1]['content'])==REQUEST
        assert not body.get('tools')
        return httpx.Response(200,json=api_response())
    ai=adapter(handler)
    try:
        result=ai.classify(REQUEST)
        assert result.category_id==CATEGORY
        assert result.input_tokens==100
        assert result.output_tokens==20
    finally:
        ai.close()


@pytest.mark.parametrize('response,code',[
    (api_response(category=str(uuid4())),'invalid_category'),
    (api_response(confidence=0.4),'low_confidence'),
    (api_response(category=None),'low_confidence'),
    (api_response(content=[{'type':'refusal','refusal':'No'}]),'incomplete_or_refused'),
    (api_response(confidence=1.5),'ValidationError'),
    (api_response(content=[{'type':'output_text','text':'{"category_id":"x"}','annotations':[]}]),'ValidationError'),
])
def test_untrusted_output_falls_back(response,code):
    ai=adapter(lambda _:httpx.Response(200,json=response))
    try:
        result=ai.classify(REQUEST)
        assert result.category_id is None
        assert result.error_code==code
    finally:
        ai.close()


@pytest.mark.parametrize('status',[401,429,500])
def test_api_errors_no_automatic_retries(status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'error':{'message':'sensitive upstream detail','type':'api_error'}})
    ai=adapter(handler)
    try:
        assert ai.classify(REQUEST).error_code
        assert len(calls)==1
    finally:
        ai.close()


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout('timeout',request=request)
    ai=adapter(handler)
    try:
        assert ai.classify(REQUEST).error_code=='APITimeoutError'
    finally:
        ai.close()


def test_normalization_and_no_partial_word_matches():
    assert normalize('  КОФЕ, с мёдом!  ')=='кофе с медом'
    rules=[{'id':'1','match_kind':'keyword','pattern':'такси','category_id':'a'}]
    assert match_rule('На такси до дома',rules)=='a'
    assert match_rule('Таксидермия',rules) is None
    rules.append({'id':'2','match_kind':'description','pattern':'на такси до дома','category_id':'b'})
    assert match_rule('На такси до дома!',rules)=='b'
