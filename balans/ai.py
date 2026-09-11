"""Provider adapter. No database access; only validated category suggestions."""
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import unicodedata

from openai import OpenAI, APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROMPT_VERSION = 'category-v1'
DEFAULT_MODEL = 'gpt-4.1-mini-2025-04-14'
PROMPT = Path(__file__).with_name('prompts').joinpath('category_v1.txt').read_text()
log = logging.getLogger('balans.ai')


class CategoryResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category_id: str | None
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class Suggestion:
    category_id: str | None = None
    confidence: float = 0
    error_code: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def normalize(text: str) -> str:
    value = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')
    return ' '.join(re.findall(r'\w+', value, re.UNICODE))


def match_rule(description, rules):
    value = normalize(description)
    # Exact corrected descriptions outrank keyword phrases; longer phrases win.
    for rule in sorted(rules, key=lambda r: (r['match_kind'] != 'description', -len(r['pattern']), str(r['id']))):
        pattern = rule['pattern']
        if (rule['match_kind'] == 'description' and value == pattern) or (
            rule['match_kind'] == 'keyword' and f' {pattern} ' in f' {value} '
        ):
            return str(rule['category_id'])
    return None


class CategoryAI:
    def __init__(self, api_key=None, model=DEFAULT_MODEL, timeout=10.0, threshold=0.75, client=None):
        self.model = model
        self.threshold = threshold
        self.client = client or (OpenAI(api_key=api_key, timeout=timeout, max_retries=0,
                                       base_url='https://api.openai.com/v1') if api_key else None)

    @classmethod
    def from_env(cls):
        timeout = float(os.getenv('AI_TIMEOUT_SECONDS', '10'))
        threshold = float(os.getenv('AI_CONFIDENCE_THRESHOLD', '0.75'))
        if not math.isfinite(timeout) or not 1 <= timeout <= 30 or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError('Некорректные AI_TIMEOUT_SECONDS или AI_CONFIDENCE_THRESHOLD')
        return cls(os.getenv('OPENAI_API_KEY', '').strip(), os.getenv('OPENAI_MODEL', DEFAULT_MODEL).strip() or DEFAULT_MODEL, timeout, threshold)

    @property
    def available(self):
        return self.client is not None

    def close(self):
        if self.client:
            self.client.close()

    def classify(self, request: dict) -> Suggestion:
        if not self.available:
            return Suggestion(error_code='not_configured')
        try:
            response = self.client.responses.parse(
                model=self.model, store=False, max_output_tokens=160,
                input=[{'role':'system', 'content':PROMPT},
                       {'role':'user', 'content':json.dumps(request, ensure_ascii=False)}],
                text_format=CategoryResult,
            )
            if response.status != 'completed' or response.output_parsed is None:
                return Suggestion(error_code='incomplete_or_refused', response_id=response.id)
            result = CategoryResult.model_validate(response.output_parsed)
            allowed = {c['id'] for c in request['categories']}
            if result.category_id is not None and result.category_id not in allowed:
                return Suggestion(error_code='invalid_category', response_id=response.id)
            usage = response.usage
            if result.category_id is None or result.confidence < self.threshold:
                return Suggestion(confidence=result.confidence, error_code='low_confidence', response_id=response.id,
                                  input_tokens=usage.input_tokens if usage else None, output_tokens=usage.output_tokens if usage else None)
            return Suggestion(result.category_id, result.confidence, response_id=response.id,
                              input_tokens=usage.input_tokens if usage else None, output_tokens=usage.output_tokens if usage else None)
        except (APIError, ValidationError, ValueError, TypeError) as exc:
            # Don't log request, response body, description or API credentials.
            code = type(exc).__name__
            log.warning('Категоризация недоступна: %s', code)
            return Suggestion(error_code=code)
