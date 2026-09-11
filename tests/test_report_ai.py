import json
import httpx
from openai import OpenAI
from balans.report_ai import ReportAI
from scripts.check_report_pdf import fixture


def test_sdk_only_receives_aggregates():
    def handler(request):
        body=json.loads(request.content)
        payload=json.loads(body['input'][1]['content'])
        assert set(payload)=={'start','end','previous_start','previous_end','summary','previous','delta','delta_percent','facts','actions'}
        assert 'Тестовая покупка' not in request.content.decode()
        assert 'Тестовый магазин' not in request.content.decode()
        assert body['store'] is False and body['text']['format']['strict'] is True
        result={'fact_ids':['recorded_total','no_comparison'],'action_ids':['complete_records']}
        return httpx.Response(200,json={'id':'resp_report','object':'response','created_at':1789000000,'status':'completed','model':'test',
             'output':[{'type':'message','id':'msg','status':'completed','role':'assistant','content':[{'type':'output_text','text':json.dumps(result),'annotations':[]}]}]})
    with OpenAI(api_key='test',http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        assert 'Проверьте, все ли расходы' in ReportAI(client,'test').analyze(fixture()).recommendations[0]
