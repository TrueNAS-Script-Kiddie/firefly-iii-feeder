"""
Phase 1 parser for Fintro's free-text 'details' column.

The details column encodes 0..N of: opposing account IBAN/BIC/name,
transaction type and timing, bank/technical references, description /
structured reference / 'zonder mededeling' marker, value date, booking ref.

Parsing is sequential and destructive: each matched segment is removed from
remaining_details so later patterns cannot match already-extracted content.
The order is determined by parsing reliability, not by the output schema.
"""

import re
from typing import Any

from engine.banks.fintro.parsers import parse_ddmmyyyy, parse_ddmmyyyy_time, parse_iban

# Reused by multiple blocks below (DOORLOPENDE OPDRACHT, OVERSCHRIJVING, MOBIELE BETALING).
# Country code + check digits are always written together (BE57, never "LN 35" from an
# address like "STATIONS-LN 12 B3"), followed by either the rest in one piece
# (NL91ABNA0417164300) or blocks of 4 with single spaces (BE68 5390 0754 7034).
RE_IBAN_BIC = re.compile(
    r"\b([A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?))"
    r"\s+BIC\s+([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
    re.IGNORECASE,
)


def record_step(trace: list[tuple[str, str, str]] | None, step: str, removed: str, remaining: str) -> None:
    """Debug trace for debug_row: what a step cut out and what is left."""
    if trace is not None:
        trace.append((step, removed, remaining))


def extract_details(
    details: str, primary_transaction_date_iso: str, trace: list[tuple[str, str, str]] | None = None
) -> dict[str, Any]:
    """
    Parse the free-text 'details' column into a dict of named fields.

    `primary_transaction_date_iso` is the already-parsed (YYYY-MM-DD) primary
    transaction date, used as (a) year-fallback for partial dates inside details
    and (b) the cutoff for the legacy 'old transaction' fallback pattern.

    Raises ValueError if unmatched residual content remains after all patterns.

    `trace`, when given, collects (step, removed text, remaining text) per matched
    step (see debug_row).
    """
    remaining_details = details

    details_booking_date = ""
    details_bank_reference = ""
    details_transaction_processing_date = ""
    details_description = ""
    details_opposing_account_name = ""
    details_transaction_type = ""
    details_payment_date = ""
    details_opposing_account_iban = ""
    details_opposing_account_bic = ""
    details_technical_reference = ""
    details_exchange_and_transaction_costs = ""
    details_no_description = False

    details_dom_date = ""
    details_match_type = ""

    # VALUTADATUM -> details_booking_date
    RE_BOOKING_DATE = re.compile(r"VALUTADATUM\s*:\s*(\d{2}/\d{2}/\d{4})$")
    match = RE_BOOKING_DATE.search(remaining_details)
    if match:
        details_booking_date = parse_ddmmyyyy(match.group(1))
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "VALUTADATUM", match.group(0), remaining_details)

    # BANKREFERENTIE -> details_bank_reference
    RE_BANK_REFERENCE = re.compile(r"BANKREFERENTIE\s*:\s*([0-9]+)$")
    match = RE_BANK_REFERENCE.search(remaining_details)
    if match:
        details_bank_reference = match.group(1)
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "BANKREFERENTIE", match.group(0), remaining_details)

    # UITGEVOERD OP -> details_transaction_processing_date
    RE_TRANSACTION_PROCESSING_DATE = re.compile(r"UITGEVOERD OP\s+(\d{2}/\d{2}(?:/\d{4})?)$")
    match = RE_TRANSACTION_PROCESSING_DATE.search(remaining_details)
    if match:
        details_transaction_processing_date = parse_ddmmyyyy(
            match.group(1), fallback_date_str=primary_transaction_date_iso
        )
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "UITGEVOERD OP", match.group(0), remaining_details)

    # MEDEDELING, TERUGBETALING WOONKREDIET, MAANDELIJKSE BIJDRAGE, BONUS, NETTO INTERESTEN, EQUIPERINGSKOSTEN,
    # GEBRUIKSKOSTEN, EFFECTEN, KREDIET -> details_description
    RE_DESCRIPTION = re.compile(
        r"MEDEDELING\s*:\s*(.*)$"  # group 1: details_description
        r"|"
        r"^(TERUGBETALING\s+WOONKREDIET(?:\s+[0-9\-]+)?)"  # group 2: details_description
        r"|"
        r"^(MAANDELIJKSE\s+BIJDRAGE(?:\s+.+)?)"  # group 3: details_description
        r"|"
        r"^(UITBETALING VAN UW BONUS [0-9]{4})(?: UW TROUW WORDT BELOOND ZIE BIJLAGE VOOR DETAILS)"
        # group 4: details_description
        r"|"
        r"^(NETTO INTERESTEN)(?:\s?\:\s?DETAILS ZIE BIJLAGE)?"  # group 5: details_description
        r"|"
        r"^((?:MAANDELIJKSE )?EQUIPERINGSKOSTEN VOOR DE PERIODE .+?)(?:\s+DETAILS ZIE BIJLAGE)?$"
        # group 6: fee description
        r"|"
        r"^(GEBRUIKSKOSTEN VOOR DE PERIODE .+?)(?:\s+DETAILS ZIE BIJLAGE)?$"  # group 7: fee description
        r"|"
        r"^(INSCHRIJVING OP BELGISCHE EFFECTEN REFERTE : [0-9]+)$"  # group 8: pensioensparen
        r"|"
        r"^((?:INTEREST|DOSSIERKOSTEN VOOR|VERVROEGDE TERUGBETALING) KREDIET( [0-9\-]+)?)$"  # group 9: krediet
    )
    match = RE_DESCRIPTION.search(remaining_details)
    if match:
        details_match_type = "Description"
        details_description = (
            match.group(1)
            or match.group(2)
            or match.group(3)
            or match.group(4)
            or match.group(5)
            or match.group(6)
            or match.group(7)
            or match.group(8)
            or match.group(9)
        ).strip()
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "DESCRIPTION", match.group(0), remaining_details)

    # ZONDER MEDEDELING -> details_no_description
    RE_NO_DESCRIPTION = re.compile(r"\bZONDER\s+MEDEDELING\b$")
    match = RE_NO_DESCRIPTION.search(remaining_details)
    if match:
        details_match_type = "No description"
        details_no_description = True
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "ZONDER MEDEDELING", match.group(0), remaining_details)

    # STORTING -> details_opposing_account_name, details_payment_date, details_transaction_type
    RE_STORTING = re.compile(
        r"^(STORTING)"  # group 1: details_transaction_type
        r"( VAN )"  # group 2: drop
        r"(.+?)"  # group 3: details_opposing_account_name
        r"( OP DE REKENING GEKOPPELD AAN DE DEBETKAART NUMMER )"  # group 4: details_transaction_type
        r"([0-9]{4}\s[0-9]{2}XX\sXXXX\s(?:[0-9]{4}|X[0-9]{3}\s[0-9]))"  # group 5: details_transaction_type
        r"( [0-9]{2}/[0-9]{2}/[0-9]{4})"  # group 6: details_payment_date
    )
    match = RE_STORTING.search(remaining_details)
    if match:
        details_match_type = "Storting"
        details_opposing_account_name = match.group(3).strip()
        details_payment_date = parse_ddmmyyyy_time(match.group(6), None)
        details_transaction_type = match.group(1) + match.group(4) + match.group(5)
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "STORTING", match.group(0), remaining_details)

    # DOORLOPENDE OPDRACHT -> details_transaction_type, details_opposing_account_name
    RE_DOORLOPENDE_OPDRACHT = re.compile(
        r"^(UW )"  # group 1: drop
        r"(DOORLOPENDE OPDRACHT)"  # group 2: details_transaction_type
        r"( TEN GUNSTE VAN REKENING )"  # group 3: drop
        r"(.+)$"  # group 4: details_opposing_account_iban/bic/name
    )
    match = RE_DOORLOPENDE_OPDRACHT.search(remaining_details)
    if match:
        details_match_type = "Doorlopende opdracht"
        details_transaction_type = match.group(2).strip()
        rest = match.group(4).strip()
        match_iban_bic = RE_IBAN_BIC.search(rest)
        if match_iban_bic:
            details_opposing_account_iban = parse_iban(match_iban_bic.group(1))
            details_opposing_account_bic = match_iban_bic.group(2)
            rest = rest.replace(match_iban_bic.group(0), "").strip()
        details_opposing_account_name = rest
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "DOORLOPENDE OPDRACHT", match.group(0), remaining_details)

    # DOMICILIERING -> details_transaction_type, details_opposing_account_name, details_dom_date,
    # details_technical_reference
    RE_DOMICILIERING = re.compile(
        r"^(?:(EERSTE INVORDERING VAN EEN ))?"  # group 1: details_transaction_type optional prefix
        r"((?:GEWEIGERDE )?EUROPESE DOMICILIERING)"  # group 2: details_transaction_type core
        r"( VAN )"  # group 3: drop
        r"(.+?)"  # group 4: details_opposing_account_name
        r"(?: DATUM : ([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}))?"  # group 5: details_dom_date optional
        r"( MANDAAT NUMMER : )"  # group 6: details_technical_reference fixed
        r"([A-Z0-9\-]+)"  # group 7: details_technical_reference mandate number
        r"( REFERTE : )"  # group 8: details_technical_reference fixed
        r"(.*)$"  # group 9: details_technical_reference reference
    )
    match = RE_DOMICILIERING.search(remaining_details)
    if match:
        details_match_type = "Domiciliering"
        details_transaction_type = ((match.group(1) or "") + match.group(2)).strip()
        details_opposing_account_name = match.group(4).strip()
        details_dom_date = (match.group(5) or "").strip()
        details_technical_reference = (match.group(6) + match.group(7) + match.group(8) + match.group(9)).strip()
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "DOMICILIERING", match.group(0), remaining_details)

    # OVERSCHRIJVING -> details_transaction_type, details_technical_reference, details_opposing_account_iban,
    # details_opposing_account_bic, details_opposing_account_name
    RE_OVERSCHRIJVING = re.compile(
        r"^"
        r"(WERO |INSTANT|EUROPESE |INSTANT EUROPESE )?"  # group 1: details_transaction_type prefix
        r"(OVERSCHRIJVING)"  # group 2: details_transaction_type core
        r"( IN EURO)?"  # group 3: drop
        r"( OP REKENING| VAN REKENING| NAAR)?"  # group 4: drop
        r"(.*?)(?= VIA WEB BANKING| VIA MOBILE BANKING| REFERTE OPDRACHTGEVER| UW REFERTE|$)"
        # group 5: details_opposing_account_iban/bic/name (part 1)
        r"( VIA WEB BANKING| VIA MOBILE BANKING)?"  # group 6: details_transaction_type via
        r"(.*?)(?= REFERTE OPDRACHTGEVER| UW REFERTE|$)"
        # group 7: details_opposing_account_iban/bic/name (part 2)
        r"(?:( REFERTE OPDRACHTGEVER| UW REFERTE)"  # group 8: details_technical_reference label
        r"( : )"  # group 9: drop
        r"(.*?))?$",  # group 10: details_technical_reference value
        re.IGNORECASE,
    )
    match = RE_OVERSCHRIJVING.search(remaining_details)
    if match:
        details_match_type = "Overschrijving"
        # details_technical_reference
        if match.group(8) and match.group(10):
            details_technical_reference = match.group(8).strip() + " : " + match.group(10).strip()
        # details_opposing_account_iban/bic/name
        rest = (match.group(5) or "").strip() + " " + (match.group(7) or "").strip()
        match_iban_bic = RE_IBAN_BIC.search(rest)
        if match_iban_bic:
            details_opposing_account_iban = parse_iban(match_iban_bic.group(1))
            details_opposing_account_bic = match_iban_bic.group(2)
            rest = rest.replace(match_iban_bic.group(0), "").strip()
        # Kind of transfer written before the name ("VIA DERDE PARTIJ KLARNA ...", "NA AFSLUITING
        # REKENING ..."): it describes the transfer, so it joins the transaction type, not the name
        kind = ""
        match_kind = re.match(r"(VIA DERDE PARTIJ|NA AFSLUITING REKENING)\s+", rest, re.IGNORECASE)
        if match_kind:
            kind = match_kind.group(1)
            rest = rest[match_kind.end() :]
        details_opposing_account_name = rest.strip()
        # details_transaction_type
        prefix = (match.group(1) or "").strip()
        core = match.group(2).strip()
        via = (match.group(6) or "").strip()
        details_transaction_type = " ".join(x for x in [prefix, core, kind, via] if x).strip()
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "OVERSCHRIJVING", match.group(0), remaining_details)

    # BETALING -> details_transaction_type, details_opposing_account_name, details_payment_date,
    # details_exchange_and_transaction_costs
    RE_BETALING = re.compile(
        r"^"
        r"((?:TERUG)?BETALING MET (?:DEBET\s?KAART(?: NUMMER)?|BANKKAART MET KAART)"
        r" [0-9]{4}\s[0-9]{2}XX\sXXXX\s(?:[0-9]{4}|X[0-9]{3}\s[0-9]))"
        # group 1: details_transaction_type (part 2)
        r"(?:( BANCONTACT PAYCONIQ)(?: CO)?)?"  # group 2: details_transaction_type (part 1 primary)
        r"(.*)"  # group 3: details_opposing_account_name
        r"( P2P MOBILE)?"  # group 4: details_transaction_type (part 1 primary)
        r"( [0-9]{2}/[0-9]{2}/[0-9]{4})"  # group 5: details_payment_date (date)
        r"( OM)?"  # group 6: drop
        r"( [0-9]{2}:[0-9]{2}| [0-9]{2} U [0-9]{2})?"  # group 7: details_payment_date (time)
        r"(.*?)"  # group 8: details_exchange_and_transaction_costs (optional free text)
        r"( BANCONTACT| VISA DEBIT - CONTACTLOOS| VISA DEBIT - eCommerce| VISA DEBIT)?"
        # group 9: details_transaction_type (part 1 secondary)
        r"$",
        re.IGNORECASE,
    )
    match = RE_BETALING.search(remaining_details)
    if match:
        details_match_type = "Betaling"
        details_transaction_type = (
            ((match.group(2) or "").strip() or (match.group(9) or "").strip())
            + " "
            + (match.group(4) or "").strip()
            + " "
            + match.group(1).strip()
        ).strip()
        details_opposing_account_name = match.group(3).strip()
        details_payment_date = parse_ddmmyyyy_time(match.group(5), match.group(7))
        details_exchange_and_transaction_costs = match.group(8).strip()
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "BETALING", match.group(0), remaining_details)

    # MOBIELE BETALING -> details_transaction_type, details_opposing_account_iban, details_opposing_account_bic,
    # details_opposing_account_name
    RE_MOBIELE_BETALING = re.compile(
        r"^"
        r"(MOBIELE BETALING)"  # group 1: transaction_type (part 2)
        r"( OPDRACHTGEVER REKENING : )"  # group 2: drop
        r"(.*)"  # group 3: iban, bic, name
        r"( BANCONTACT)"  # group 4: transaction_type (part 1)
        r"$",
        re.IGNORECASE,
    )
    match = RE_MOBIELE_BETALING.search(remaining_details)
    if match:
        details_match_type = "Mobiele betaling"
        details_transaction_type = match.group(4).strip() + " " + match.group(1).strip()
        rest = match.group(3).strip()
        match_iban_bic = RE_IBAN_BIC.search(rest)
        if match_iban_bic:
            details_opposing_account_iban = parse_iban(match_iban_bic.group(1))
            details_opposing_account_bic = match_iban_bic.group(2)
            rest = rest.replace(match_iban_bic.group(0), "").strip()
        details_opposing_account_name = rest
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "MOBIELE BETALING", match.group(0), remaining_details)

    # GELDOPNEMING -> details_transaction_type, details_opposing_account_name, details_payment_date
    RE_GELDOPNEMING = re.compile(
        r"^"
        r"(GELDOPN(?:EMING|AME))"  # group 1: transaction_type verb
        r"(?: IN EURO)?"  # drop
        r"((?: AAN (?:ANDERE|ONZE) AUTOMATEN(?: BE)?)?"
        r" MET (?:DEBETKAART NUMMER|KAART) [0-9]{4}\s[0-9]{2}XX\sXXXX\s(?:[0-9]{4}|X[0-9]{3}\s[0-9]))"
        # group 2: transaction_type suffix
        r"(.*)"  # group 3: details_opposing_account_name
        r"( [0-9]{2}/[0-9]{2}/[0-9]{4})"  # group 4: details_payment_date
        r"( [0-9]{2}:[0-9]{2}| [0-9]{2} U [0-9]{2})?"  # group 5: details_payment_date (time)
        r"( BANCONTACT| VISA DEBIT)?"  # group 6: transaction_type (part 1) — absent in older transactions
        r"$",
        re.IGNORECASE,
    )
    match = RE_GELDOPNEMING.search(remaining_details)
    if match:
        details_match_type = "Geldopneming"
        details_transaction_type = (
            (match.group(6) or "").strip() + " " + (match.group(1) + match.group(2)).strip()
        ).strip()
        details_opposing_account_name = match.group(3).strip()
        details_payment_date = parse_ddmmyyyy_time(match.group(4), match.group(5))
        remaining_details = remaining_details.replace(match.group(0), "").strip()
        record_step(trace, "GELDOPNEMING", match.group(0), remaining_details)

    # Old transactions -> details_transaction_type, details_opposing_account_name
    if remaining_details and primary_transaction_date_iso < "2018-09-01":
        RE_CARDNUMBER = re.compile(
            r"^"
            r"([0-9]{4}\s[0-9]{2}XX\sXXXX\s[0-9]{4})?"  # group 1: details_transaction_type
            r"(?:\s([0-9]))?"  # group 2: drop
            r"(.*)"  # group 3: details_opposing_account_name
            r"$"
        )
        match = RE_CARDNUMBER.search(remaining_details)
        if match:
            details_match_type = "Old transaction"
            if match.group(1):
                details_transaction_type = match.group(1).strip()
            details_opposing_account_name = match.group(3).strip()
            record_step(trace, "OLD TRANSACTION", remaining_details, "")
            remaining_details = ""

    # Remaining details
    if remaining_details and remaining_details not in details_description:
        if details_match_type:
            raise ValueError(
                f"Regex match ({details_match_type}), but remaining_details should be empty instead of '{remaining_details}'"  # noqa: E501
            )
        else:
            raise ValueError(f"remaining_details should be empty instead of '{remaining_details}'")

    return {
        "booking_date": details_booking_date,
        "bank_reference": details_bank_reference,
        "transaction_processing_date": details_transaction_processing_date,
        "description": details_description,
        "opposing_account_name": details_opposing_account_name,
        "transaction_type": details_transaction_type,
        "payment_date": details_payment_date,
        "opposing_account_iban": details_opposing_account_iban,
        "opposing_account_bic": details_opposing_account_bic,
        "technical_reference": details_technical_reference,
        "exchange_and_transaction_costs": details_exchange_and_transaction_costs,
        "no_description": details_no_description,
        "dom_date": details_dom_date,
    }
