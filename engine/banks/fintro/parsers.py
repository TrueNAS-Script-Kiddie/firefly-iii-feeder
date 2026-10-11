"""
Pure parsing helpers for Fintro (the shared ones are in engine.core.parsers):
- Day-first date parsing ('dd/mm/yyyy' and 'dd/mm' with year fallback)
- Amount in another currency with rate and costs
"""

import re
from datetime import date
from decimal import Decimal

from engine.core.parsers import parse_comma_decimal_amount


def parse_ddmmyyyy(
    value: str,
    fallback_date_str: str | None = None,
) -> str:
    """
    Parse 'dd/mm' or 'dd/mm/yyyy' into 'YYYY-MM-DD'.
    """

    fallback_date = None
    if fallback_date_str is not None:
        fallback_date = date.fromisoformat(fallback_date_str)

    parts = value.split("/")
    if len(parts) == 3:
        day, month, year = map(int, parts)
        return date(year, month, day).isoformat()

    if len(parts) == 2:
        if fallback_date is None:
            raise ValueError(f"Missing year in date and no fallback_date provided: {value}")

        day, month = map(int, parts)

        candidates = []
        for year in (fallback_date.year - 1, fallback_date.year, fallback_date.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                pass

        if not candidates:
            raise ValueError(f"Could not construct a valid date from: {value}")

        final_date = min(candidates, key=lambda d: abs(d - fallback_date))
        return final_date.isoformat()

    raise ValueError(f"Invalid date format: {value}")


def parse_ddmmyyyy_time(date_value: str, time_value: str | None) -> str:
    """
    Parse 'dd/mm/yyyy' plus optional 'HH:MM' / 'HH U MM' into 'YYYY-MM-DD[ HH:MM]'.
    No time means unknown, so none is invented.
    """
    iso_date = parse_ddmmyyyy(date_value.strip())
    time_value = (time_value or "").replace(" U ", ":").strip()
    if not time_value:
        return iso_date
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_value):
        raise ValueError(f"Invalid time format: {time_value}")
    return f"{iso_date} {time_value}"


RE_FOREIGN_AMOUNT = re.compile(
    r"([A-Z]{3}) (\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"  # currency, amount
    r"(?: KOERS (\d+,\d+))?"  # rate
    r"((?: [A-Z]+: (?:\d{1,3}(?:\.\d{3})+|\d+),\d{2} EUR)*)"  # costs: WISSELKOSTEN: 0,47 EUR ...
)


def parse_foreign_amount(value: str) -> tuple[str, Decimal, Decimal | None, Decimal] | None:
    """
    'SEK 330,00 KOERS 11,497000 WISSELKOSTEN: 0,47 EUR' -> ('SEK', 330.00, 11.497000, 0.47 = sum of the costs).
    None when the text does not start with an amount ('BEHANDELINGSKOSTEN: 4,71 EUR'); rate None when absent.
    Raises ValueError when it starts with an amount but does not fit this form.
    """
    if not re.match(r"[A-Z]{3} \d", value):
        return None
    match = RE_FOREIGN_AMOUNT.fullmatch(value)
    if not match:
        raise ValueError(f"Amount in details in an unknown form: '{value}'")
    costs = sum((parse_comma_decimal_amount(c) for c in re.findall(r": (\S+) EUR", match.group(4))), Decimal(0))
    rate = parse_comma_decimal_amount(match.group(3)) if match.group(3) else None
    return match.group(1), parse_comma_decimal_amount(match.group(2)), rate, costs
