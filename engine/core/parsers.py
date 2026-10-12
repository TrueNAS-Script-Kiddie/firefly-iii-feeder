"""
Parsing/formatting helpers shared by the banks and the importer:
- IBAN parsing/validation, with checksum
- Belgian account number from before IBAN ('123-1234567-12') -> IBAN
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


def belgian_account_to_iban(value: str) -> str:
    """
    Belgian account number of before 2014, '123-1234567-12', -> its IBAN 'BEnn123123456712'.
    Raises ValueError on another form or a wrong check: the last two digits are the first ten
    mod 97, 97 when that is 0.
    """
    if not re.fullmatch(r"[0-9]{3}-[0-9]{7}-[0-9]{2}", value):
        raise ValueError(f"Not a Belgian account number: '{value}'")
    digits = value.replace("-", "")
    if (int(digits[:10]) % 97 or 97) != int(digits[10:]):
        raise ValueError(f"Belgian account number with a wrong check: '{value}'")
    # ISO 13616 check digits: the number, then 'BE' as 1114 and '00', mod 97
    check = 98 - int(f"{digits}111400") % 97
    return parse_iban(f"BE{check:02d}{digits}")


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
