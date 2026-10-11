"""
Argenta normalize_row orchestrator (current and savings accounts, xlsx export).

Pipeline per row:

  PHASE 1 — EXTRACT
    1a. Pull values out of the columns; look up the rule of the row's Beschrijving.
    1b. Card rows: parse the Mededeling (engine.banks.argenta.extract_card_details).

  PHASE 2 — RECONCILE, ASSEMBLE
    Check what is stated twice (booking date in the Referentie, direction in the Beschrijving
    and the sign, name in column and Mededeling), place the counterparty per rule, assemble.

Every Beschrijving has a rule in RULES; an unknown one fails the row.
"""

import re
from dataclasses import dataclass
from typing import Any

from engine.banks.argenta.extract_card_details import extract_card_details
from engine.banks.argenta.parsers import parse_exact_amount, parse_reference_date, statement_date_from_day_number
from engine.core.parsers import extract_structured_ref, normalize_for_comparison, parse_iban

BANK_COUNTERPARTY_NAME = "Argenta"
CARD_COUNTERPARTY_NAME = "Argenta Mastercard"

# Argenta's placeholder counterparty on card rows; its checksum is valid, so Firefly would put
# every shop on one account
DUMMY_IBAN = "BE54000000000000"


@dataclass(frozen=True)
class Rule:
    # Type line of notes, in Fintro's wording where Fintro has the same kind
    transaction_type: str
    # Required sign of the amount: "-", "+", or None for either
    sign: str | None
    # How the counterparty columns are read:
    #   columns           IBAN and name as given
    #   card              dummy IBAN dropped; name from Naam tegenpartij and the parsed Mededeling
    #   card_settlement   IBAN as given (the card's collection account); name CARD_COUNTERPARTY_NAME
    #   bank              no counterparty given; name BANK_COUNTERPARTY_NAME
    #   own_account       the row's own IBAN, dropped; name BANK_COUNTERPARTY_NAME
    #   deposit           term deposit number -> opposing_account_number
    #   deposit_interest  deposit number, repeated in the Mededeling, -> notes; name BANK_COUNTERPARTY_NAME
    counterparty: str
    is_cash_withdrawal: bool = False


RULES = {
    # Inkomende/Uitgaande only repeats the sign of the amount (checked)
    "Inkomende overschrijving": Rule("Overschrijving", "+", "columns"),
    "Uitgaande overschrijving": Rule("Overschrijving", "-", "columns"),
    "Inkomende Instantoverschrijving": Rule("Instantoverschrijving", "+", "columns"),
    "Uitgaande instantoverschrijving": Rule("Instantoverschrijving", "-", "columns"),
    "Betaling Maestro": Rule("Maestro betaling met debetkaart", "-", "card"),
    "Betaling België": Rule("Betaling met debetkaart", "-", "card"),
    "Betaling bancontact in het buitenl.": Rule("Buitenlandse Bancontact betaling met debetkaart", "-", "card"),
    "Opname Maestro": Rule("Maestro geldopneming met debetkaart", "-", "card", is_cash_withdrawal=True),
    "Opname bancontact in het buitenland": Rule(
        "Buitenlandse Bancontact geldopneming met debetkaart", "-", "card", is_cash_withdrawal=True
    ),
    "Debet ten voordele van BCC": Rule("Debet ten voordele van BCC", "-", "card_settlement"),
    "Storting kredietkaart": Rule("Storting kredietkaart", "+", "columns"),
    "Kredietkaart Green-pakket": Rule("Kredietkaart Green-pakket", "-", "bank"),
    "Taks Portkost Interest": Rule("Taks Portkost Interest", None, "own_account"),
    "Start van een termijndeposito": Rule("Start van een termijndeposito", "-", "deposit"),
    "Vereffening van termijnplaatsing": Rule("Vereffening van termijnplaatsing", "+", "deposit"),
    "Vereffening van een termijndeposito": Rule("Vereffening van een termijndeposito", "+", "deposit_interest"),
}

# Statement settlement: 'MASTERCARD  247' = day number of the statement date
RE_CARD_SETTLEMENT_NAME = re.compile(r"^MASTERCARD\s+([0-9]{3})$")
# Term deposit interest: '<deposit number> - <code>'; the code's meaning is unknown, so it stays
RE_DEPOSIT_INTEREST_MEDEDELING = re.compile(r"^(\S+) - (\S+)$")


def collapse_spaces(value: str) -> str:
    """Names padded to a fixed width ('AB         C') -> 'AB C': formatting, not information."""
    return " ".join(value.split())


def rule_for(transaction_type: str) -> Rule:
    if transaction_type not in RULES:
        raise ValueError(f"Unknown Beschrijving: '{transaction_type}'")
    return RULES[transaction_type]


def normalize_row(csv_row: dict[str, str]) -> dict[str, Any]:

    # ==================================================================
    # PHASE 1a — EXTRACT COLUMNS
    # ==================================================================

    column_external_id = csv_row["external_id"]
    column_primary_transaction_date = csv_row["primary_transaction_date"]
    column_interest_date = csv_row["interest_date"]
    column_transaction_processing_date = csv_row["transaction_processing_date"]
    column_asset_account_iban = parse_iban(csv_row["asset_account_iban"])
    column_amount = parse_exact_amount(csv_row["amount"])
    column_account_currency_code = csv_row["account_currency_code"]
    column_opposing_account = csv_row.get("opposing_account_iban", "").strip()
    column_opposing_account_name = collapse_spaces(csv_row.get("opposing_account_name", ""))
    column_description = csv_row.get("description", "").strip()
    rule = rule_for(csv_row["transaction_type"])

    # ==================================================================
    # PHASE 1b — EXTRACT CARD MEDEDELING
    # ==================================================================

    details = extract_card_details(column_description) if rule.counterparty == "card" else None

    # ==================================================================
    # PHASE 2 — RECONCILE, ASSEMBLE
    # ==================================================================

    normalized: dict[str, Any] = {
        "external_id": "",  # 1
        "primary_transaction_date": "",  # 2
        "transaction_processing_date": "",  # 3
        "interest_date": "",  # 4
        "payment_date": "",  # 5
        "amount": "",  # 6
        "account_currency_code": "",  # 7
        "foreign_amount": "",  # 8
        "foreign_currency_code": "",  # 9
        "asset_account_iban": "",  # 10
        "opposing_account_iban": "",  # 11
        "opposing_account_bic": "",  # 12
        "opposing_account_number": "",  # 13
        "opposing_account_name": "",  # 14
        "is_cash_withdrawal": "",  # 15
        "description": "",  # 16
        "notes": "",  # 17
        "unmapped_exchange_and_transaction_costs": "",  # 18
        "unmapped_transaction_type": "",  # 19
        "unmapped_reference_parts": "",  # 20
    }

    # column_external_id &| column_primary_transaction_date (coded in the Referentie, match) -> external_id
    reference_date = parse_reference_date(column_external_id)
    if reference_date != column_primary_transaction_date:
        raise ValueError(
            f"Booking date mismatch: Referentie '{column_external_id}' codes {reference_date}, "
            f"Boekdatum is {column_primary_transaction_date}"
        )
    normalized["external_id"] = column_external_id

    # column dates -> primary_transaction_date, interest_date, transaction_processing_date
    normalized["primary_transaction_date"] = column_primary_transaction_date
    normalized["interest_date"] = column_interest_date
    normalized["transaction_processing_date"] = column_transaction_processing_date

    # column_amount &| rule.sign (Inkomende/Uitgaande, Debet, …) -> amount
    if (rule.sign == "-" and column_amount >= 0) or (rule.sign == "+" and column_amount <= 0):
        raise ValueError(f"Amount {column_amount} has the wrong sign for '{csv_row['transaction_type']}'")
    normalized["amount"] = str(column_amount)

    # column_account_currency_code -> account_currency_code
    if column_account_currency_code != "EUR":
        raise ValueError("Non-EUR account currency")
    normalized["account_currency_code"] = column_account_currency_code

    # column_asset_account_iban -> asset_account_iban
    normalized["asset_account_iban"] = column_asset_account_iban

    exchange_parts: list[str] = []
    reference_parts: list[str] = []
    transaction_type = rule.transaction_type

    # column_opposing_account &| column_opposing_account_name &| column_description, per rule -> counterparty
    if rule.counterparty in ("columns", "card_settlement"):
        opposing_account_iban = parse_iban(
            column_opposing_account, error_message=f"Invalid IBAN in Rekening tegenpartij: '{column_opposing_account}'"
        )
        if opposing_account_iban == DUMMY_IBAN:
            raise ValueError(f"Dummy IBAN on a row that is no card payment: '{column_opposing_account}'")
        if rule.counterparty == "card_settlement":
            if not opposing_account_iban:
                raise ValueError("Card settlement without the card's collection account")
            # Old rows leave the name empty: the statement date is then unknown, the card is not
            if column_opposing_account_name:
                match = RE_CARD_SETTLEMENT_NAME.match(column_opposing_account_name)
                if not match:
                    raise ValueError(f"Card settlement name in an unknown form: '{column_opposing_account_name}'")
                statement_date = statement_date_from_day_number(match.group(1), column_primary_transaction_date)
                reference_parts.append(f"Card statement of {statement_date}")
            normalized["opposing_account_name"] = CARD_COUNTERPARTY_NAME
        else:
            normalized["opposing_account_name"] = column_opposing_account_name
        normalized["opposing_account_iban"] = opposing_account_iban
        normalized["description"] = extract_structured_ref(column_description) or column_description

    elif rule.counterparty == "card":
        if parse_iban(column_opposing_account) != DUMMY_IBAN:
            raise ValueError(f"Card row with a counterparty other than the dummy IBAN: '{column_opposing_account}'")
        # The name column is one part of the Mededeling: the shop before the date, or the place after it.
        # Text before the date that is not the name (the ATM's bank) belongs to the name too.
        name_norm = normalize_for_comparison(column_opposing_account_name)
        if not name_norm or name_norm not in (
            normalize_for_comparison(details["before_date"]),
            normalize_for_comparison(details["after_date"]),
        ):
            raise ValueError(
                f"Opposing account name mismatch: column='{column_opposing_account_name}' "
                f"Mededeling='{column_description}'"
            )
        normalized["opposing_account_name"] = collapse_spaces(f"{details['before_date']} {details['after_date']}")
        normalized["payment_date"] = details["payment_date"]
        if details["card_number"]:
            transaction_type = f"{transaction_type} {details['card_number']}"
        # EUR repeats the account currency; another one has no amount, so it can only be a note
        if details["currency"] and details["currency"] != column_account_currency_code:
            exchange_parts.append(f"Munt: {details['currency']}")

    elif rule.counterparty in ("bank", "own_account"):
        if rule.counterparty == "bank" and (column_opposing_account or column_opposing_account_name):
            raise ValueError(
                f"Bank cost with a counterparty: '{column_opposing_account}' '{column_opposing_account_name}'"
            )
        if rule.counterparty == "own_account" and (
            parse_iban(column_opposing_account) != column_asset_account_iban or column_opposing_account_name
        ):
            raise ValueError(
                f"Interest with a counterparty other than the own account: "
                f"'{column_opposing_account}' '{column_opposing_account_name}'"
            )
        normalized["opposing_account_name"] = BANK_COUNTERPARTY_NAME
        normalized["description"] = extract_structured_ref(column_description) or column_description

    elif rule.counterparty in ("deposit", "deposit_interest"):
        if not re.fullmatch(r"[A-Z0-9]+", column_opposing_account) or column_opposing_account_name:
            raise ValueError(
                f"Term deposit without a deposit number as counterparty: "
                f"'{column_opposing_account}' '{column_opposing_account_name}'"
            )
        if rule.counterparty == "deposit":
            normalized["opposing_account_number"] = column_opposing_account
            normalized["description"] = column_description
        else:
            match = RE_DEPOSIT_INTEREST_MEDEDELING.match(column_description)
            if not match or match.group(1) != column_opposing_account:
                raise ValueError(
                    f"Term deposit interest: Mededeling '{column_description}' does not name the deposit "
                    f"'{column_opposing_account}'"
                )
            reference_parts.append(f"Term deposit: {match.group(1)} {match.group(2)}")
            normalized["opposing_account_name"] = BANK_COUNTERPARTY_NAME

    else:
        raise ValueError(f"Rule without a known counterparty kind: '{rule.counterparty}'")

    # mix -> notes + unmapped columns
    notes_parts = [" ".join(exchange_parts), transaction_type, " ".join(reference_parts)]
    normalized["notes"] = "\n".join(part for part in notes_parts if part)
    normalized["unmapped_exchange_and_transaction_costs"] = " ".join(exchange_parts)
    normalized["unmapped_transaction_type"] = transaction_type
    normalized["unmapped_reference_parts"] = " ".join(reference_parts)

    # The importer books it on Firefly's Cash account
    if rule.is_cash_withdrawal:
        normalized["is_cash_withdrawal"] = "1"

    return normalized
