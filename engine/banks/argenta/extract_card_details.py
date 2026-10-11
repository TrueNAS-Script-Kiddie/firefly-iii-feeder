"""
Phase 1 parser for the Mededeling of Argenta's card rows (payments and withdrawals).

Two layouts (examples made up):
  new: '[SHOP ]dd-mm-yyyy hh:mm PLACE[ COUNTRY] CARDNR'
       'ACME MARKT 17-06-2023 11:00 GLASGOW GB 123456*******7890'
       '17-06-2023 11:00 GLASGOW 123456*******7890'
  old: 'CURRENCY NAME   dd-mm-yy hh:mm', NAME padded with spaces
       'USD SAINT-VAAST        08-05-17 13:01'

Parsing is sequential and destructive, as for Fintro's details: each matched part is cut from
the remaining text. Which part is the counterparty name is decided in phase 2, against the
Naam tegenpartij column.
"""

import re
from typing import Any

from engine.banks.argenta.parsers import parse_card_date_time

RE_NEW_CARD_NUMBER = re.compile(r" ([0-9]{6}\*+[0-9]{4})$")
RE_NEW_DATE_TIME = re.compile(r"(?:^|(?<= ))([0-9]{2}-[0-9]{2}-[0-9]{4}) ([0-9]{2}:[0-9]{2})(?= )")
RE_OLD_DATE_TIME = re.compile(r" +([0-9]{2}-[0-9]{2}-[0-9]{2}) ([0-9]{2}:[0-9]{2})$")
RE_OLD_CURRENCY = re.compile(r"^([A-Z]{3}) ")


def record_step(trace: list[tuple[str, str, str]] | None, step: str, removed: str, remaining: str) -> None:
    """Debug trace for debug_trace.py: what a step cut out and what is left."""
    if trace is not None:
        trace.append((step, removed, remaining))


def extract_card_details(mededeling: str, trace: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    """
    Split a card row's Mededeling into its parts. Raises ValueError when it fits neither layout
    or a part is missing.
    """
    remaining = mededeling.strip()

    details_card_number = ""
    details_payment_date = ""
    details_currency = ""
    details_before_date = ""
    details_after_date = ""

    if RE_NEW_DATE_TIME.search(remaining):
        # CARDNR -> details_card_number
        match = RE_NEW_CARD_NUMBER.search(remaining)
        if not match:
            raise ValueError(f"Card Mededeling without a card number at the end: '{mededeling}'")
        details_card_number = match.group(1)
        remaining = remaining[: match.start()].strip()
        record_step(trace, "CARD NUMBER", match.group(0), remaining)

        # dd-mm-yyyy hh:mm -> details_payment_date; the text around it is shop and place
        match = RE_NEW_DATE_TIME.search(remaining)
        if not match:
            raise ValueError(f"Card Mededeling: no place between date and card number: '{mededeling}'")
        details_payment_date = parse_card_date_time(match.group(1), match.group(2))
        details_before_date = remaining[: match.start()].strip()
        details_after_date = remaining[match.end() :].strip()
        remaining = ""
        record_step(trace, "DATE TIME", match.group(0), remaining)
        record_step(trace, "BEFORE DATE", details_before_date, remaining)
        record_step(trace, "AFTER DATE", details_after_date, remaining)
        if not details_after_date:
            raise ValueError(f"Card Mededeling without a place after the date: '{mededeling}'")

    elif RE_OLD_DATE_TIME.search(remaining):
        # dd-mm-yy hh:mm -> details_payment_date
        match = RE_OLD_DATE_TIME.search(remaining)
        details_payment_date = parse_card_date_time(match.group(1), match.group(2))
        remaining = remaining[: match.start()].strip()
        record_step(trace, "DATE TIME", match.group(0), remaining)

        # CURRENCY -> details_currency
        match = RE_OLD_CURRENCY.search(remaining)
        if not match:
            raise ValueError(f"Card Mededeling in the old layout without a currency: '{mededeling}'")
        details_currency = match.group(1)
        remaining = remaining[match.end() :].strip()
        record_step(trace, "CURRENCY", match.group(0), remaining)

        details_after_date = remaining
        remaining = ""
        record_step(trace, "NAME", details_after_date, remaining)
        if not details_after_date:
            raise ValueError(f"Card Mededeling in the old layout without a name: '{mededeling}'")

    else:
        raise ValueError(f"Card Mededeling fits no known layout: '{mededeling}'")

    return {
        "card_number": details_card_number,
        "payment_date": details_payment_date,
        "currency": details_currency,
        "before_date": details_before_date,
        "after_date": details_after_date,
    }
