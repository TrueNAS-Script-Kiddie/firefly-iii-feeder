"""
Pure parsing helpers for Argenta (the shared ones are in engine.core.parsers):
- Booking date coded in the Referentie
- Exact amount from the xlsx number text
- Card statement date from the day number in 'MASTERCARD  247'
- Day-first card dates ('dd-mm-yyyy' and 'dd-mm-yy') with a time
"""

import re
from datetime import date, timedelta
from decimal import Decimal

# A card statement is debited about ten days after its date; a day number further back is misread
MAX_STATEMENT_TO_DEBIT_DAYS = 31


def parse_reference_date(reference: str) -> str:
    """
    Booking date coded in a Referentie, 'C6I30…' -> '2026-09-30': decade letter (A = 2000s,
    B = 2010s, C = 2020s), year digit, month letter (A = January … L = December), day.
    """
    match = re.match(r"([A-Z])([0-9])([A-L])([0-9]{2})", reference)
    if not match:
        raise ValueError(f"Referentie does not start with a coded date: '{reference}'")
    year = 2000 + 10 * (ord(match.group(1)) - ord("A")) + int(match.group(2))
    month = ord(match.group(3)) - ord("A") + 1
    try:
        return date(year, month, int(match.group(4))).isoformat()
    except ValueError:
        raise ValueError(f"Referentie codes no valid date: '{reference}'") from None


def parse_exact_amount(value: str) -> Decimal:
    """'-12.5' -> Decimal('-12.50'); more than two decimals is not an amount in cents."""
    amount = Decimal(value)
    if amount.as_tuple().exponent < -2:
        raise ValueError(f"Amount with more than two decimals: '{value}'")
    return amount.quantize(Decimal("0.01"))


def statement_date_from_day_number(day_number: str, booking_date_iso: str) -> str:
    """
    Day number of the statement date (001-366) -> ISO date, in the year that puts it shortly
    before the booking date (a debit in January can pay a December statement).
    """
    booking_date = date.fromisoformat(booking_date_iso)
    for year in (booking_date.year, booking_date.year - 1):
        candidate = date(year, 1, 1) + timedelta(days=int(day_number) - 1)
        if candidate.year == year and timedelta(0) <= booking_date - candidate <= timedelta(
            days=MAX_STATEMENT_TO_DEBIT_DAYS
        ):
            return candidate.isoformat()
    raise ValueError(
        f"Statement day number {day_number} is no date within {MAX_STATEMENT_TO_DEBIT_DAYS} days before "
        f"the booking date {booking_date_iso}"
    )


def parse_card_date_time(date_value: str, time_value: str) -> str:
    """'17-06-2023' or '17-06-23', '11:00' -> '2023-06-17 11:00'."""
    day, month, year = date_value.split("-")
    full_year = int(year) if len(year) == 4 else 2000 + int(year)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_value):
        raise ValueError(f"Invalid time format: {time_value}")
    return f"{date(full_year, int(month), int(day)).isoformat()} {time_value}"
