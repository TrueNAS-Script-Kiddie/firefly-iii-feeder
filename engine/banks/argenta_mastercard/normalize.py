"""
Argenta Mastercard normalize_row orchestrator (one statement line, as statement.py reads it).

Pipeline per row:

  PHASE 1 — EXTRACT
    Pull values out of the columns; the line's kind from its printed text (repayment, fee,
    transfer to a bank account, reversal), the reader's cash-withdrawal mark, or else a purchase
    or refund by its sign.

  PHASE 2 — RECONCILE, ASSEMBLE
    Check what is stated twice (the foreign amount against the rate and the EUR amount), place
    the counterparty per kind, assemble.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from engine.banks.argenta_mastercard.parsers import (
    FEE_TEXT,
    RE_TRANSFER_OUT,
    REPAYMENT_TEXTS,
    REVERSAL_TEXT,
    parse_amount,
    parse_signed,
)
from engine.core.parsers import apply_replacements, belgian_account_to_iban

BANK_COUNTERPARTY_NAME = "Argenta"

TYPE_PURCHASE = "Betaling met kredietkaart"
TYPE_REFUND = "Terugbetaling met kredietkaart"
TYPE_REPAYMENT = "Afbetaling kredietkaart"
TYPE_TRANSFER_OUT = "Overdracht kredietkaart"
TYPE_CASH_WITHDRAWAL = "Geldopneming met kredietkaart"
TYPE_REVERSAL = "Terugboeking betaling met kredietkaart"

# Printed English text -> type line in the wording of the other card lines
REPLACE_IN_STATEMENT_TEXT = [
    (REVERSAL_TEXT, TYPE_REVERSAL),
]
TYPE_FEE = "Kosten op geldopneming"


def collapse_spaces(value: str) -> str:
    """Text padded to a fixed width ('AB         C') -> 'AB C': formatting, not information."""
    return " ".join(value.split())


def line_kind(statement_text: str, amount: Decimal, is_cash_withdrawal: str) -> str:
    if statement_text in REPAYMENT_TEXTS:
        return "repayment"
    if statement_text == FEE_TEXT:
        return "fee"
    if RE_TRANSFER_OUT.match(collapse_spaces(statement_text)):
        return "transfer"
    if statement_text == REVERSAL_TEXT:
        return "reversal"
    if is_cash_withdrawal == "1":
        return "cash withdrawal"
    return "purchase" if amount < 0 else "refund"


def normalize_row(csv_row: dict[str, str]) -> dict[str, Any]:

    # ==================================================================
    # PHASE 1 — EXTRACT COLUMNS
    # ==================================================================

    column_asset_account_number = csv_row["asset_account_number"]
    column_settlement_date = csv_row["primary_transaction_date"]
    column_transaction_date = csv_row["payment_date"]
    column_statement_text = csv_row["statement_text"].strip()
    column_amount = parse_signed(csv_row["amount"])
    column_account_currency_code = csv_row["account_currency_code"]
    column_foreign_amount = csv_row["foreign_amount"]
    column_foreign_currency_code = csv_row["foreign_currency_code"]
    column_exchange_rate = csv_row["exchange_rate"]
    column_card_number = csv_row["card_number"].strip()
    column_card_holder = collapse_spaces(csv_row["card_holder"])
    column_is_cash_withdrawal = csv_row["is_cash_withdrawal"]
    kind = line_kind(column_statement_text, column_amount, column_is_cash_withdrawal)

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
        "asset_account_number": "",  # 21
        "is_card_repayment": "",  # 23 (22 = row_key, set by process_csv)
        "is_payment_reversal": "",  # 24
    }

    # column_settlement_date | column_transaction_date (repayment line with one date) -> primary_transaction_date
    if not column_settlement_date and kind != "repayment":
        raise ValueError("Card line without a settlement date")
    normalized["primary_transaction_date"] = column_settlement_date or column_transaction_date

    # column_transaction_date -> payment_date
    normalized["payment_date"] = column_transaction_date

    # column_amount &| kind (repayment, reversal +; fee, transfer, cash withdrawal -) -> amount
    if not column_amount:
        raise ValueError("Amount is zero")
    if (kind in ("repayment", "reversal") and column_amount < 0) or (
        kind in ("fee", "transfer", "cash withdrawal") and column_amount > 0
    ):
        raise ValueError(f"Amount {column_amount} has the wrong sign for a {kind} line")
    normalized["amount"] = str(column_amount)

    # column_account_currency_code -> account_currency_code
    if column_account_currency_code != "EUR":
        raise ValueError("Non-EUR account currency")
    normalized["account_currency_code"] = column_account_currency_code

    # column_asset_account_number -> asset_account_number (a card account has no IBAN)
    normalized["asset_account_number"] = column_asset_account_number

    exchange_parts: list[str] = []
    reference_parts: list[str] = []

    # column_foreign_amount &| column_exchange_rate &| column_amount (foreign ÷ rate = EUR) -> foreign_amount
    if column_foreign_amount or column_foreign_currency_code or column_exchange_rate:
        if not (column_foreign_amount and column_foreign_currency_code and column_exchange_rate):
            raise ValueError("Foreign amount, currency and rate: all three or none")
        if kind not in ("purchase", "refund", "cash withdrawal"):
            raise ValueError(f"Foreign amount on a {kind} line")
        foreign_amount = parse_amount(column_foreign_amount)
        in_eur = (foreign_amount / parse_amount(column_exchange_rate)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if in_eur != abs(column_amount):
            raise ValueError(
                f"Rate mismatch: {column_foreign_amount} {column_foreign_currency_code} at "
                f"1 EUR={column_exchange_rate} is {in_eur} EUR, the line says {abs(column_amount)}"
            )
        normalized["foreign_amount"] = str(-foreign_amount if column_amount < 0 else foreign_amount)
        normalized["foreign_currency_code"] = column_foreign_currency_code
        exchange_parts.append(f"Koers: {column_exchange_rate} {column_foreign_currency_code}")

    # column_statement_text &| kind -> counterparty, transaction type
    if kind == "repayment":
        if column_card_number or column_card_holder:
            raise ValueError("Repayment line under a card")
        # The paying account is not on the statement: the importer takes the card's paid_from
        normalized["is_card_repayment"] = "1"
        transaction_type = f"{TYPE_REPAYMENT}: {column_statement_text}"
    else:
        if not column_card_holder:
            raise ValueError("Card line without a card holder")
        if kind == "fee":
            # Argenta's own fee for the cash withdrawal on the line above
            normalized["opposing_account_name"] = BANK_COUNTERPARTY_NAME
            transaction_type = TYPE_FEE
        elif kind == "transfer":
            # The own account it went to, so the importer claims the transfer its export created
            account = RE_TRANSFER_OUT.match(collapse_spaces(column_statement_text)).group(1)
            normalized["opposing_account_iban"] = belgian_account_to_iban(account)
            transaction_type = f"{TYPE_TRANSFER_OUT}: {collapse_spaces(column_statement_text)}"
        elif kind == "reversal":
            # Counterparty left empty: the importer takes it from the payment this reverses
            normalized["is_payment_reversal"] = "1"
            transaction_type = apply_replacements(column_statement_text, REPLACE_IN_STATEMENT_TEXT)
        elif kind == "cash withdrawal":
            # The importer books it on Firefly's Cash account; the ATM's name goes into the description
            normalized["opposing_account_name"] = collapse_spaces(column_statement_text)
            normalized["is_cash_withdrawal"] = "1"
            transaction_type = TYPE_CASH_WITHDRAWAL
        else:
            normalized["opposing_account_name"] = collapse_spaces(column_statement_text)
            transaction_type = TYPE_PURCHASE if kind == "purchase" else TYPE_REFUND
        # column_card_number &| column_card_holder -> notes (one statement prints no card number)
        reference_parts.append(" ".join(part for part in ("Kaart", column_card_number, column_card_holder) if part))

    # mix -> notes + unmapped columns
    notes_parts = [" ".join(exchange_parts), transaction_type, " ".join(reference_parts)]
    normalized["notes"] = "\n".join(part for part in notes_parts if part)
    normalized["unmapped_exchange_and_transaction_costs"] = " ".join(exchange_parts)
    normalized["unmapped_transaction_type"] = transaction_type
    normalized["unmapped_reference_parts"] = " ".join(reference_parts)

    return normalized
