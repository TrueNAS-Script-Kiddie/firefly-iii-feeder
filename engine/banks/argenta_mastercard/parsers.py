"""
Helpers shared by the statement reader and normalize_row:
- Printed texts that name a line kind
- Amounts as printed ('1.221,59', '1.221,59-')
"""

import re
from decimal import Decimal

REPAYMENT_TEXTS = {"Domiciliëring bij uw bank", "DOMICILIERING VIA UW BANK"}
FEE_TEXT = "KOSTEN OP GELDOPNEMING"
# Reversal of an earlier card payment; the trailing ' -' is part of the bank's text
REVERSAL_TEXT = "Refund of First presentment -"
# Credit balance paid out to a bank account, named by its pre-IBAN Belgian number
RE_TRANSFER_OUT = re.compile(r"^OVERDRACHT NAAR BANKREKENING ([0-9]{3}-[0-9]{7}-[0-9]{2})$")


def parse_amount(text: str) -> Decimal:
    """'1.221,59' -> Decimal('1221.59')."""
    return Decimal(text.replace(".", "").replace(",", "."))


def parse_signed(text: str) -> Decimal:
    """'1.221,59-' -> Decimal('-1221.59'), as the reader writes Bedrag."""
    amount = parse_amount(text[:-1])
    return -amount if text.endswith("-") else amount
