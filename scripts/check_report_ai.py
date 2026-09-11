"""Live API check with generated aggregates, never real financial history."""
from dotenv import load_dotenv
from openai import APIError
from balans.ai import CategoryAI
from balans.report_ai import ReportAI
from scripts.check_report_pdf import fixture

if __name__=='__main__':
    load_dotenv()
    client=CategoryAI.from_env()
    try:
        if not client.available:raise SystemExit('OPENAI_API_KEY не настроен')
        result=ReportAI(client.client,client.model).analyze(fixture())
        print(result.model_dump_json(indent=2))
        print('OK: синтетический отчёт; финансовые данные пользователей не читались.')
    except APIError as exc:
        raise SystemExit(type(exc).__name__) from None
    finally:
        client.close()
