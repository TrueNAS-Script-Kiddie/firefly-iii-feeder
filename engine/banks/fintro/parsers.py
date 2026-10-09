"""
Pure parsing/formatting helpers for Fintro:
- IBAN parsing/validation
- Day-first date parsing ('dd/mm/yyyy' and 'dd/mm' with year fallback)
- Amounts with a decimal comma and thousands dots ('1.234,56')
- Amount in another currency with rate and costs
- Structured reference (+++nnn/nnnn/nnnnn+++) handling
- Accent-insensitive string comparison
- Sequential string replacement
"""

import re
import unicodedata
from datetime import date
from decimal import Decimal


def parse_iban(value: str, *, error_message: str | None = None) -> str:
    # Accept empty string as-is
    if value == "":
        return ""

    iban = re.sub(r"\s+", "", value).upper()

    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        msg = error_message or f"Invalid IBAN: '{value}' -> '{iban}'"
        raise ValueError(msg)

    return iban


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


def parse_comma_decimal_amount(value: str) -> Decimal | None:
    """'1.234,56' or '1234,56' -> Decimal('1234.56'); None when it is not such an amount."""
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+|\d+,\d+", value):
        return None
    return Decimal(value.replace(".", "").replace(",", "."))


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


def canonicalize_structured_ref(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 12:
        raise ValueError("Invalid structured reference")
    return f"+++{digits[0:3]}/{digits[3:7]}/{digits[7:12]}+++"


def extract_structured_ref(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()

    # already formatted
    if re.fullmatch(r"\+{3}\d{3}/\d{4}/\d{5}\+{3}", value):
        return value

    # raw 12 digits
    if re.fullmatch(r"\d{12}", value):
        return canonicalize_structured_ref(value)

    return None


def normalize_for_comparison(value: str) -> str:
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", "", value).upper()


def apply_replacements(value: str, replacements: list[tuple[str, str]]) -> str:
    for old, new in replacements:
        value = value.replace(old, new)
    return value
