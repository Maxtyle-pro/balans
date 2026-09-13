"""Structured recognition of financial text; no ledger access."""
import json
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field
from balans.voice_ai import VoiceResult,validate_result

PROMPT=Path(__file__).with_name('prompts').joinpath('text_v1.txt').read_text()

class TextResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operations:list[VoiceResult]=Field(max_length=8)

class TextAI:
    def __init__(self,client,model):self.client=client;self.model=model
    @property
    def available(self):return self.client is not None
    def extract(self,request):
        response=self.client.with_options(timeout=30,max_retries=0).responses.parse(model=self.model,store=False,max_output_tokens=3000,input=[{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(request,ensure_ascii=False)}],text_format=TextResult)
        if response.status!='completed' or response.output_parsed is None:raise ValueError('Incomplete text recognition')
        result=TextResult.model_validate(response.output_parsed)
        result.operations=[validate_result(x,request['categories'],request['message_date']) for x in result.operations]
        return result, getattr(response.usage,'input_tokens',None),getattr(response.usage,'output_tokens',None)
