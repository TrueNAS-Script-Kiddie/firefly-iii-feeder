"""
Parsing/formatting helpers shared by the banks and the importer:
- IBAN parsing/validation, with checksum
- Amounts with a decimal comma and thousands dots ('1.234,56')
- Structured reference (+++nnn/nnnn/nnnnn+++) handling
- Accent-insensitive string comparison
- Sequential string replacement
"""

import re
import unicodedata
from decimal import Decimal


def parse_iban(value: str, *, error_message: str | None = None) -> str:
    """IBAN without whitespace, upper case; "" stays "". Raises ValueError on a wrong form or checksum."""
    if value == "":
        return ""

    iban = re.sub(r"\s+", "", value).upper()

    # ISO 13616 checksum: country code and check digits moved to the end, letters as 10..35, mod 97 = 1
    if (
        not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban)
        or int("".join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97 != 1
    ):
        msg = error_message or f"Invalid IBAN: '{value}' -> '{iban}'"
        raise ValueError(msg)

    return iban


def parse_comma_decimal_amount(value: str) -> Decimal | None:
    """'1.234,56' or '1234,56' -> Decimal('1234.56'); None when it is not such an amount."""
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+|\d+,\d+", value):
        return None
    return Decimal(value.replace(".", "").replace(",", "."))


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
