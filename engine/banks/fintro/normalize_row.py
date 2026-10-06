"""
Fintro normalize_row orchestrator.

Pipeline per row:

  PHASE 1 — EXTRACT
    1a. Pull values out of dedicated CSV columns.
    1b. Parse the free-text 'details' column (engine.banks.fintro.extract_details).

  PHASE 2 — RECONCILE, REFORMAT, ASSEMBLE
    2a. Reconcile values that appear in both column and details sources.
    2b. Reformat cosmetic text (REPLACE_IN_* tables, card-number mask).
    2c. Assemble the normalized output dict.

All regex/string parsing lives in extract_details. All cross-source decisions
live in reconcile. This file contains only orchestration and the reformat
tables that shape the user-visible notes text.
"""

import re
from typing import Any

from engine.banks.fintro.extract_details import extract_details
from engine.banks.fintro.parsers import (
    apply_replacements,
    extract_structured_ref,
    normalize_for_comparison,
    parse_ddmmyyyy,
    parse_iban,
)
from engine.banks.fintro.reconcile import (
    merge_opposing_account_name,
    reconcile_transaction_types,
)

# ----------------------------------------------------------------------
# Reformat tables (applied during notes assembly)
# ----------------------------------------------------------------------

REPLACE_IN_DETAILS_DESCRIPTION = [
    ("NETTO INTERESTEN", "Netto interesten : "),
    ("MAANDELIJKSE BIJDRAGE FINTRO BLUE", "Maandelijkse bijdrage voor Fintro Blue"),
    ("TERUGBETALING WOONKREDIET", "Terugbetaling woonkrediet"),
    ("UITBETALING VAN UW BONUS", "Uitbetaling van uw bonus"),
    ("MAANDELIJKSE EQUIPERINGSKOSTEN VOOR DE PERIODE", "Maandelijkse equiperingskosten voor de periode"),
    (" TOT ", " tot "),
    ("GEBRUIKSKOSTEN VOOR DE PERIODE", "Gebruikskosten voor de periode"),
    (
        "INSCHRIJVING OP BELGISCHE EFFECTEN REFERTE",
        "Inschrijving op de Belgische effecten (pensioensparen), referte",
    ),
    ("INTEREST KREDIET", "Interest krediet"),
    ("DOSSIERKOSTEN VOOR KREDIET", "Dossierkosten voor krediet"),
    ("VERVROEGDE TERUGBETALING KREDIET", "Vervroegde terugbetaling krediet"),
]

REPLACE_IN_DETAILS_EXCHANGE_AND_TRANSACTION_COSTS = [
    ("BEHANDELINGSKOSTEN", "Behandelingskosten"),
    ("KOERS", "Koers:"),
    ("WISSELKOSTEN", "Wisselkosten"),
]

REPLACE_IN_DETAILS_TRANSACTION_TYPE = [
    ("  ", " "),
    ("ANNULERING BETALING MET DEBETKAART NUMMER", "Annulering betaling met debetkaart"),
    ("ANNULERING BETALING MET KAART", "Annulering betaling met debetkaart"),
    ("BETALING MET BANKKAART MET KAART", "Betaling met debetkaart"),
    ("BETALING MET DEBET KAART NUMMER", "Betaling met debetkaart"),
    ("BETALING MET DEBETKAART NUMMER", "Betaling met debetkaart"),
    ("GELDOPNEMING MET DEBETKAART NUMMER", "Geldopneming met debetkaart"),
    (
        "GELDOPNAME IN BUITENLAND AAN ANDERE AUTOMATEN MET KAART",
        "Buitenlandse geldopneming aan andere automaten met debetkaart",
    ),
    (
        "GELDOPNEMING IN BUITENLAND AAN ANDERE AUTOMATEN MET DEBETKAART NUMMER",
        "Buitenlandse geldopneming aan andere automaten met debetkaart",
    ),
    ("GELDOPNAME AAN ANDERE AUTOMATEN MET KAART", "Geldopneming aan andere automaten met debetkaart"),
    ("GELDOPNAME AAN ONZE AUTOMATEN MET KAART", "Geldopneming aan onze automaten met debetkaart"),
    ("GELDOPNEMING AAN ANDERE AUTOMATEN MET KAART", "Geldopneming aan andere automaten met debetkaart"),
    ("GELDOPNEMING AAN ONZE AUTOMATEN MET KAART", "Geldopneming aan onze automaten met debetkaart"),
    (
        "GELDOPNEMING AAN ANDERE AUTOMATEN MET DEBETKAART NUMMER",
        "Geldopneming aan andere automaten met debetkaart",
    ),
    ("GELDOPNEMING AAN ONZE AUTOMATEN MET DEBETKAART NUMMER", "Geldopneming aan onze automaten met debetkaart"),
    (
        "GELDOPNEMING AAN ONZE AUTOMATEN BE MET DEBETKAART NUMMER",
        "Geldopneming aan onze automaten met debetkaart",
    ),
    ("INSTANT EUROPESE OVERSCHRIJVING", "Instantoverschrijving"),
    ("INSTANT OVERSCHRIJVING", "Instantoverschrijving"),
    ("INSTANTOVERSCHRIJVING", "Instantoverschrijving"),
    ("EERSTE INVORDERING VAN EEN EUROPESE DOMICILIERING", "Domiciliëring, eerste invordering"),
    ("EUROPESE DOMICILIERING", "Domiciliëring"),
    ("EUROPESE OVERSCHRIJVING", "Overschrijving"),
    ("VIA WEB BANKING", "via Web Banking"),
    ("VIA MOBILE BANKING", "via Mobile Banking"),
    ("VIA DERDE PARTIJ", "via derde partij"),
    ("NA AFSLUITING REKENING", "na afsluiting rekening"),
    ("OVERSCHRIJVING", "Overschrijving"),
    ("TERUGBETALING MET DEBETKAART", "Terugbetaling met debetkaart"),
    ("BANCONTACT P2P MOBILE Betaling", "P2P mobile Bancontact betaling"),
    ("P2P MOBILE Betaling", "P2P mobile betaling"),
    ("BANCONTACT Betaling", "Bancontact betaling"),
    ("BANCONTACT Terugbetaling", "Bancontact terugbetaling"),
    ("BANCONTACT Geldopneming", "Bancontact geldopneming"),
    ("BANCONTACT PAYCONIQ Betaling", "Payconiq Bancontact betaling"),
    ("WERO Instantoverschrijving", "Wero instantoverschrijving"),
    ("VISA DEBIT - CONTACTLOOS Betaling", "Contactloze Visa betaling"),
    ("VISA DEBIT - eCommerce Betaling", "Online Visa betaling"),
    ("VISA DEBIT Betaling", "Visa betaling"),
    ("VISA DEBIT Geldopneming", "Visa geldopneming"),
    ("BANCONTACT MOBIELE KAARTBETALING", "Mobiele Bancontact betaling"),
    (
        "STORTING OP DE REKENING GEKOPPELD AAN DE DEBETKAART NUMMER",
        "Storting op de rekening gekoppeld aan de debetkaart",
    ),
]

REPLACE_IN_COLUMN_TRANSACTION_TYPE = [
    ("Diverse Debetverrichtingen", "Diverse debetverrichtingen"),
    ("Hypotheekleningen Terugbetalingen", "Terugbetaling hypotheeklening"),
    ("Kaartbetaling", "Betaling met debetkaart"),
    ("Geldopname met kaart", "Geldopneming met debetkaart"),
    ("Overschrijving buitenland", "Buitenlandse overschrijving"),
]

# 'Type verrichting' values of transactions with Fintro itself (loans, fees, interest, bonus).
# Fintro leaves the counterparty empty for these.
BANK_COUNTERPARTY_NAME = "Fintro"
BANK_COUNTERPARTY_TRANSACTION_TYPES = {
    "Aflossing krediet",
    "Diverse correcties",
    "Diverse Debetverrichtingen",
    "Effecteninschrijving",
    "Hypotheekleningen Terugbetalingen",
    "Interesten op zichtrekening",
    "Kosten diverse verrichtingen",
    "Kosten in verband met de rekening",
    "Kosten rekeningbeheer",
    "Terugbetalingen",
}

REPLACE_IN_DETAILS_TECHNICAL_REFERENCE = [
    ("MANDAAT NUMMER :", "Mandaat nummer:"),
    ("REFERTE OPDRACHTGEVER :", "Referte opdrachtgever:"),
    ("UW REFERTE :", "Uw referte:"),
    ("REFERTE :", "Referte:"),
]


# ----------------------------------------------------------------------
# Main normalize function
# ----------------------------------------------------------------------


def normalize_row(csv_row: dict[str, str]) -> dict[str, Any]:

    # ==================================================================
    # PHASE 1a — EXTRACT COLUMNS
    # ==================================================================

    column_external_id = csv_row["external_id"]
    column_amount = csv_row["amount"]
    column_account_currency_code = csv_row["account_currency_code"]
    column_asset_account_iban = csv_row["asset_account_iban"]
    raw_opposing_account_iban = csv_row.get("opposing_account_iban", "").strip()
    column_opposing_account_number = ""
    # Foreign account numbers without an IBAN (123456789012) are kept as a plain number; anything
    # IBAN-shaped must be a valid IBAN, so a broken one is not passed off as a number
    if re.fullmatch(r"[0-9A-Z]+", raw_opposing_account_iban, re.IGNORECASE) and not re.match(
        r"[A-Z]{2}[0-9]{2}", raw_opposing_account_iban, re.IGNORECASE
    ):
        column_opposing_account_number = raw_opposing_account_iban.upper()
        raw_opposing_account_iban = ""
    column_opposing_account_iban = parse_iban(
        raw_opposing_account_iban, error_message=f"Invalid IBAN in tegenpartij column: '{raw_opposing_account_iban}'"
    )
    column_opposing_account_name = csv_row.get("opposing_account_name", "").strip()
    column_description = csv_row.get("description", "").strip()
    column_transaction_type = csv_row["transaction_type"].replace(" in euro", "")
    column_primary_transaction_date = parse_ddmmyyyy(csv_row["primary_transaction_date"])
    column_booking_date = parse_ddmmyyyy(csv_row["booking_date"])

    # ==================================================================
    # PHASE 1b — EXTRACT DETAILS
    # ==================================================================

    details = extract_details(csv_row.get("details", ""), column_primary_transaction_date)

    # ==================================================================
    # PHASE 2 — RECONCILE, REFORMAT, ASSEMBLE
    # Map extracted values to normalized output fields.
    # All cross-source reconciliation and suppression logic lives here.
    # ==================================================================

    normalized: dict[str, Any] = {
        "external_id": "",  # 1
        "primary_transaction_date": "",  # 2
        "transaction_processing_date": "",  # 3
        "booking_date": "",  # 4
        "payment_date": "",  # 5
        "amount": "",  # 6
        "account_currency_code": "",  # 7
        "asset_account_iban": "",  # 8
        "opposing_account_iban": "",  # 9
        "opposing_account_bic": "",  # 10
        "opposing_account_number": "",  # 11
        "opposing_account_name": "",  # 12
        "is_cash_withdrawal": "",  # 13
        "description": "",  # 14
        "notes": "",  # 15
        "unmapped_exchange_and_transaction_costs": "",  # 16
        "unmapped_transaction_type": "",  # 17
        "unmapped_reference_parts": "",  # 18
    }

    # column_external_id -> external_id
    normalized["external_id"] = column_external_id

    # column_primary_transaction_date -> primary_transaction_date
    normalized["primary_transaction_date"] = column_primary_transaction_date

    # details.transaction_processing_date -> transaction_processing_date
    normalized["transaction_processing_date"] = details["transaction_processing_date"]

    # column_booking_date &| details.booking_date (match) -> booking_date
    if details["booking_date"] and details["booking_date"] != column_booking_date:
        raise ValueError(
            "Value date mismatch between dedicated column and details: "
            f"column_booking_date='{column_booking_date}' details_booking_date='{details['booking_date']}'"
        )
    normalized["booking_date"] = column_booking_date

    # details.payment_date -> payment_date
    normalized["payment_date"] = details["payment_date"]

    # column_amount -> amount
    normalized["amount"] = column_amount.replace(",", ".").strip()

    # column_account_currency_code -> account_currency_code
    if column_account_currency_code != "EUR":
        raise ValueError("Non-EUR account currency")
    normalized["account_currency_code"] = column_account_currency_code

    # column_asset_account_iban -> asset_account_iban
    normalized["asset_account_iban"] = column_asset_account_iban.replace(" ", "").upper()

    # column_opposing_account_iban &| details.opposing_account_iban (optional match) -> opposing_account_iban
    if (
        column_opposing_account_iban
        and details["opposing_account_iban"]
        and column_opposing_account_iban != details["opposing_account_iban"]
    ):
        raise ValueError(
            "IBAN mismatch between CSV and details: "
            f"column_opposing_account_iban='{column_opposing_account_iban}' "
            f"details_opposing_account_iban='{details['opposing_account_iban']}'"
        )
    normalized["opposing_account_iban"] = column_opposing_account_iban or details["opposing_account_iban"]

    # column_opposing_account_number -> opposing_account_number
    normalized["opposing_account_number"] = column_opposing_account_number

    # details.opposing_account_bic -> opposing_account_bic
    normalized["opposing_account_bic"] = details["opposing_account_bic"]

    # column_opposing_account_name &| details.opposing_account_name (merge) -> opposing_account_name
    normalized["opposing_account_name"] = merge_opposing_account_name(
        column_opposing_account_name, details["opposing_account_name"]
    )
    # A place without a shop (card cancellation) follows the name: "ACME PAYMENTS BREDA"
    if details["place"]:
        normalized["opposing_account_name"] = f"{normalized['opposing_account_name']} {details['place']}".strip()
    if (
        not normalized["opposing_account_name"]
        and not normalized["opposing_account_iban"]
        and csv_row["transaction_type"] in BANK_COUNTERPARTY_TRANSACTION_TYPES
    ):
        normalized["opposing_account_name"] = BANK_COUNTERPARTY_NAME

    # column_description &| details.description &| column_structured_ref &| details_structured_ref -> description
    if details["no_description"] and (column_description or details["description"]):
        raise ValueError("ZONDER MEDEDELING present but description found in a dedicated column or details column")
    details_description = apply_replacements(details["description"], REPLACE_IN_DETAILS_DESCRIPTION)
    column_structured_ref = extract_structured_ref(column_description)
    details_structured_ref = extract_structured_ref(details_description)
    if column_structured_ref or details_structured_ref:
        if column_structured_ref and details_structured_ref and column_structured_ref != details_structured_ref:
            raise ValueError(
                f"Structured reference mismatch: csv='{column_structured_ref}' details='{details_structured_ref}'"
            )
        normalized["description"] = column_structured_ref or details_structured_ref
    elif column_description:
        if details_description:
            if normalize_for_comparison(column_description) != normalize_for_comparison(details_description):
                if "Netto interesten : " in details_description:
                    normalized["description"] = details_description + column_description
                else:
                    raise ValueError(
                        f"Mededeling mismatch: column='{column_description}' details='{details_description}'"
                    )
            else:
                normalized["description"] = column_description
        else:
            normalized["description"] = column_description
    else:
        normalized["description"] = details_description
        if (
            details_description.startswith("Uitbetaling van uw bonus")
            and column_transaction_type == "Kosten rekeningbeheer"
        ):
            column_transaction_type = "Opbrengsten in verband met de rekening"

    # mix -> notes + unmapped columns
    notes_parts: list[str] = []
    reference_parts: list[str] = []

    # details.exchange_and_transaction_costs -> notes
    details_exchange_and_transaction_costs = details["exchange_and_transaction_costs"]
    if details_exchange_and_transaction_costs:
        details_exchange_and_transaction_costs = apply_replacements(
            details_exchange_and_transaction_costs, REPLACE_IN_DETAILS_EXCHANGE_AND_TRANSACTION_COSTS
        )
        details_eatc_parts = details_exchange_and_transaction_costs.split()
        if len(details_eatc_parts) != 2 or not (
            details_eatc_parts[0] == column_account_currency_code
            and float(details_eatc_parts[1].replace(",", ".")) == abs(float(column_amount.replace(",", ".")))
        ):
            if re.match(r"^[A-Za-z]{3} \d", details_exchange_and_transaction_costs):
                sign = "-" if column_amount.startswith("-") else ""
                details_exchange_and_transaction_costs = (
                    "Bedrag: " + details_eatc_parts[0] + " " + sign + " ".join(details_eatc_parts[1:])
                )
            notes_parts.append(details_exchange_and_transaction_costs)
            normalized["unmapped_exchange_and_transaction_costs"] = details_exchange_and_transaction_costs

    # Reconcile column_transaction_type and details.transaction_type
    column_transaction_type, details_transaction_type = reconcile_transaction_types(
        column_transaction_type,
        details["transaction_type"],
        details_description,
        details["dom_date"],
    )

    # details_transaction_type -> notes
    if details_transaction_type:
        details_transaction_type = apply_replacements(details_transaction_type, REPLACE_IN_DETAILS_TRANSACTION_TYPE)
        # Any other way of paying than the ones in the table, as Fintro writes it: "Google Pay Visa betaling"
        details_transaction_type = re.sub(r"VISA DEBIT - (.+?) Betaling", r"\1 Visa betaling", details_transaction_type)
        RE_NORMALIZE_CARDNUMBER = re.compile(r"\b(\d{4} \d{2}XX XXXX) X(\d{3}) (\d)\b")
        details_transaction_type = RE_NORMALIZE_CARDNUMBER.sub(
            lambda match: f"{match.group(1)} {match.group(2)}{match.group(3)}", details_transaction_type
        )
        notes_parts.append(details_transaction_type)

    # column_transaction_type -> notes
    if column_transaction_type:
        column_transaction_type = apply_replacements(column_transaction_type, REPLACE_IN_COLUMN_TRANSACTION_TYPE)
        notes_parts.append(column_transaction_type)

    # details.bank_reference -> notes
    if details["bank_reference"]:
        reference_parts.append(f"Bankreferentie: {details['bank_reference']}")

    # details.technical_reference -> notes
    details_technical_reference = details["technical_reference"]
    if details_technical_reference:
        details_technical_reference = apply_replacements(
            details_technical_reference, REPLACE_IN_DETAILS_TECHNICAL_REFERENCE
        )
        reference_parts.append(details_technical_reference)

    if reference_parts:
        notes_parts.append(" ".join(reference_parts))

    normalized["notes"] = "\n".join(notes_parts)
    normalized["unmapped_transaction_type"] = details_transaction_type or column_transaction_type
    normalized["unmapped_reference_parts"] = " ".join(reference_parts)

    # Cash withdrawal (money out) -> is_cash_withdrawal: the importer books it on Firefly's Cash account.
    # The wording is Fintro's, so this is where it belongs, not in the importer.
    if normalized["amount"].startswith("-") and "geldopn" in normalized["unmapped_transaction_type"].lower():
        normalized["is_cash_withdrawal"] = "1"

    return normalized
