"""Speech recognition and strict, expense-only extraction without database access."""
from datetime import date
import json
import os
from pathlib import Path
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field
from balans.domain import amount_from_text

PROMPT = Path(__file__).with_name('prompts').joinpath('voice_v1.txt').read_text()
DEFAULT_TRANSCRIBE_MODEL = 'gpt-4o-mini-transcribe'


class VoiceResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['expense', 'other', 'multiple']
    amount: str | None
    amount_confidence: float = Field(ge=0, le=1)
    currency: str | None
    description: str = Field(max_length=500)
    occurred_on: str | None
    date_note: str = Field(max_length=200)
    category_id: str | None
    category_confidence: float = Field(ge=0, le=1)


def mask(text):
    return re.sub(r'(?<!\d)(?:\d[ -]?){12,19}(?!\d)', '[реквизиты скрыты]', text)


def validate_result(result, categories, today):
    result = VoiceResult.model_validate(result)
    if result.amount is not None:
        amount_from_text(result.amount)
    if result.amount_confidence < .75:
        result.amount = None
    if result.category_id not in {str(c['id']) for c in categories} or result.category_confidence < .75:
        result.category_id = None
    if result.occurred_on:
        try:
            if date.fromisoformat(result.occurred_on) > date.fromisoformat(today):
                result.occurred_on = None
        except ValueError:
            result.occurred_on = None
    result.description = mask(result.description)
    result.date_note = mask(result.date_note)
    return result


class VoiceAI:
    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.transcribe_model = os.getenv('OPENAI_TRANSCRIBE_MODEL', DEFAULT_TRANSCRIBE_MODEL)

    @property
    def available(self):
        return self.client is not None

    def transcribe(self, wav):
        response = self.client.with_options(timeout=60, max_retries=0).audio.transcriptions.create(
            model=self.transcribe_model, file=('voice.wav', wav, 'audio/wav'),
            language='ru', response_format='json')
        text = response.text.strip()
        if not text or len(text) > 6000:
            raise ValueError('Empty or excessive transcript')
        return mask(text)

    def extract(self, transcript, categories, today):
        response = self.client.with_options(timeout=30, max_retries=0).responses.parse(
            model=self.model, store=False, max_output_tokens=1200,
            input=[{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps(
                {'transcript': transcript, 'categories': categories, 'message_date': today}, ensure_ascii=False)}],
            text_format=VoiceResult)
        if response.status != 'completed' or response.output_parsed is None:
            raise ValueError('Incomplete or refused')
        return validate_result(response.output_parsed, categories, today)
