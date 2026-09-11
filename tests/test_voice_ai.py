import json
import httpx
from openai import OpenAI, APIError
import pytest
from balans.voice_ai import VoiceAI


def test_sdk_transcribe_and_parse():
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/transcriptions'):
            assert b'voice.wav' in request.content and b'name="language"\r\n\r\nru' in request.content
            assert b'audio/wav' in request.content
            return httpx.Response(200,json={'text':'Вчера потратил 850 рублей на продукты'})
        body=json.loads(request.content)
        assert body['store'] is False and body['text']['format']['strict'] is True
        assert set(json.loads(body['input'][1]['content']))=={'transcript','categories','message_date'}
        payload=dict(kind='expense',amount='850',amount_confidence=.99,currency='RUB',description='Продукты',occurred_on='2026-09-09',date_note='Вчера',category_id='category',category_confidence=.99)
        return httpx.Response(200,json={'id':'resp_voice','object':'response','created_at':1789000000,'status':'completed','model':'test','output':[{'type':'message','id':'msg','status':'completed','role':'assistant','content':[{'type':'output_text','text':json.dumps(payload),'annotations':[]}]}]})
    with OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        ai=VoiceAI(client,'test');text=ai.transcribe(b'RIFFtest')
        result=ai.extract(text,[{'id':'category','name':'Продукты'}],'2026-09-10')
        assert result.amount=='850'
    assert len(calls)==2


@pytest.mark.parametrize('status',[401,429,500])
def test_transcription_errors_no_paid_retry(status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'error':{'message':'test','type':'test'}})
    with OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(APIError): VoiceAI(client,'test').transcribe(b'RIFFtest')
    assert len(calls)==1
