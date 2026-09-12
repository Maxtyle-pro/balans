from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from contextvars import ContextVar
CURRENCY=ContextVar("balans_currency",default="RUB")
from zoneinfo import ZoneInfo


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    invoice_id: str | None = None
    renewal_invoice_id: str | None = None
    renewal_enabled: bool | None = None
    share_id: str | None = None
    access_workspace_id: str | None = None
    attachment_id: str | None = None
    deleted_attachment_id: str | None = None
    report_id: str | None = None
    report_format: str | None = None
    report_job_id: str | None = None
    sheets_connection_id: str | None = None
    generated_document: str | None = None
    generated_filename: str | None = None
    job_id: str | None = None
    voice_job_id: str | None = None
    receipt_job_id: str | None = None
    receipt_operation_id: str | None = None
    document_ids: list[str] = field(default_factory=list)
    photo_ids: list[str] = field(default_factory=list)
    parse_mode: str | None = None


def amount_from_text(value: str) -> Decimal:
    value = value.strip().replace('\u00a0', ' ').replace('\u202f', ' ')
    if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?: [0-9]{3})+)(?:[.,][0-9]{1,2})?', value):
        raise ValueError('Введите положительную сумму, например 850 или 1 200,50. Валюта — RUB.')
    amount = Decimal(value.replace(' ', '').replace(',', '.'))
    if not Decimal('0') < amount <= Decimal('999999999999.99'):
        raise ValueError('Сумма должна быть от 0,01 до 999 999 999 999,99 RUB.')
    return amount


def date_from_text(value: str, sent_at: datetime, timezone: str) -> date:
    today = sent_at.astimezone(ZoneInfo(timezone)).date()
    if value.lower() in ('сегодня', 'вчера'):
        return today - timedelta(days=value.lower() == 'вчера')
    try:
        result = datetime.strptime(value, '%d.%m.%Y').date()
    except ValueError:
        raise ValueError('Введите «сегодня», «вчера» или дату ДД.ММ.ГГГГ.') from None
    if result > today:
        raise ValueError('Будущая дата: здесь учитываются уже совершённые расходы.')
    return result


def money(value: Decimal,currency=None) -> str:
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',') + (' ₽' if (currency or CURRENCY.get())=='RUB' else ' '+(currency or CURRENCY.get()))
