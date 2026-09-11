"""Use a synthetic recording only. Never reads bot history or creates expenses."""
import argparse
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal
from dotenv import load_dotenv
from openai import APIError
from balans.ai import CategoryAI
from balans.voice_ai import VoiceAI
from balans.voice_media import prepare_voice


def main():
    parser=argparse.ArgumentParser(description='Проверка голоса через API на синтетической записи')
    parser.add_argument('audio',type=Path)
    args=parser.parse_args()
    load_dotenv()
    client=CategoryAI.from_env()
    try:
        voice=VoiceAI(client.client,client.model)
        if not voice.available:
            raise SystemExit('OPENAI_API_KEY не настроен')
        transcript=voice.transcribe(prepare_voice(args.audio.read_bytes()))
        categories=[{'id':'11111111-1111-1111-1111-111111111111','name':'Продукты'},
                    {'id':'22222222-2222-2222-2222-222222222222','name':'Другое'}]
        result=voice.extract(transcript,categories,date.today().isoformat())
        print('Расшифровка:',transcript)
        print(result.model_dump_json(indent=2))
        if result.kind!='expense' or Decimal(result.amount or '0')!=Decimal('850') or result.currency!='RUB' or result.occurred_on!=(date.today()-timedelta(days=1)).isoformat():
            raise SystemExit('Ожидается синтетический пример: вчера потратила 850 рублей на продукты')
        print('OK: распознавание и разбор. Финансовые записи не создавались.')
    except APIError as exc:
        raise SystemExit('API недоступен: '+type(exc).__name__) from None
    finally:
        client.close()


if __name__=='__main__':
    main()
