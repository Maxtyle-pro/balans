from datetime import datetime, timezone, date
from decimal import Decimal
import pytest
from balans.domain import amount_from_text, date_from_text


@pytest.mark.parametrize('text,value', [('850','850'), ('1 200,50','1200.50'), ('0.01','0.01'), ('1\u202f200.10','1200.10')])
def test_amount(text,value):
    assert amount_from_text(text)==Decimal(value)


@pytest.mark.parametrize('text', ['0','-1','1,234','1e3','NaN','inf','1 20','2,5 тыс','1000000000000','Кофе 250','1 2 3',''])
def test_reject_ambiguous_amount(text):
    with pytest.raises(ValueError):
        amount_from_text(text)


def test_timezone_and_relative_date():
    sent = datetime(2026,9,9,22,30,tzinfo=timezone.utc)
    assert date_from_text('сегодня',sent,'Europe/Moscow')==date(2026,9,10)
    assert date_from_text('вчера',sent,'Europe/Moscow')==date(2026,9,9)
    with pytest.raises(ValueError):
        date_from_text('11.09.2026',sent,'Europe/Moscow')
    with pytest.raises(ValueError):
        date_from_text('31.02.2026',sent,'Europe/Moscow')
