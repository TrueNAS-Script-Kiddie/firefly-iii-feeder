"""
Phase 2 reconciliation helpers for Fintro.

Some fields appear in BOTH a dedicated CSV column AND inside the free-text
'details' column. These helpers compare the two sources, decide what to keep
or merge, and raise on genuine conflicts.
"""

import re

from engine.banks.fintro.parsers import normalize_for_comparison


def merge_opposing_account_name(column_value: str, details_value: str) -> str:
    """
    Return the opposing account name, merging the CSV column and the details
    column when both are present.

    Rules (normalized, accent- and whitespace-insensitive):
    - If one side is empty, the other is returned as-is.
    - If column is fully contained in details and details starts with it,
      return 'column + remaining tail of details'. A leading filler "VAN" in
      details is dropped first when that makes it start with the column.
    - Otherwise raise ValueError: text before the column name, or a different
      name, would be lost or guessed; the fix belongs in the parser.
    """
    if not column_value:
        return details_value
    if not details_value:
        return column_value

    column_norm = normalize_for_comparison(column_value)
    details_norm = normalize_for_comparison(details_value)

    if column_norm not in details_norm:
        raise ValueError(f"Opposing account name mismatch: column='{column_value}' details='{details_value}'")

    # "VAN" is a filler in some transfer variants ("EUROPESE OVERSCHRIJVING VAN ACME VZW ...") but
    # also the start of names ("VAN DER MEULEN TOM"); only the column name can tell them apart.
    if not details_norm.startswith(column_norm) and details_value.upper().startswith("VAN "):
        without_filler = details_value[4:].lstrip()
        if normalize_for_comparison(without_filler).startswith(column_norm):
            details_value, details_norm = without_filler, normalize_for_comparison(without_filler)

    if not details_norm.startswith(column_norm):
        raise ValueError(
            "Opposing account name: details has text before the column name "
            f"(would be lost): column='{column_value}' details='{details_value}'"
        )

    tail_norm_len = len(details_norm) - len(column_norm)
    if tail_norm_len <= 0:
        return column_value

    # Map the end of the column prefix (in normalized space) back to an index
    # in the original details string, so we can take the remaining tail.
    consumed = 0
    cut_index = 0
    for index, char in enumerate(details_value):
        if not char.isspace():
            consumed += 1
        if consumed >= len(column_norm):
            cut_index = index + 1
            break

    tail = details_value[cut_index:].strip()
    if not tail:
        return column_value
    return f"{column_value} {tail}".strip()


def reconcile_transaction_types(
    column_transaction_type: str,
    details_transaction_type: str,
    details_description: str,
    details_dom_date: str,
) -> tuple[str, str]:
    """
    Reconcile 'transaction_type' values from the CSV column and the details
    column. Returns (column_transaction_type, details_transaction_type) after
    reconciliation — at least one of them will be empty for the caller to
    assemble into 'notes'.

    Raises ValueError if the CSV column is empty, or if both values remain
    non-empty after all known reconciliation rules (that indicates a new
    pattern that needs a rule added here).
    """
    if not column_transaction_type:
        raise ValueError("Missing transaction type")

    column_transaction_type_norm = normalize_for_comparison(column_transaction_type).removesuffix("INEURO")

    if details_transaction_type:
        details_transaction_type_norm = normalize_for_comparison(details_transaction_type)
        if column_transaction_type_norm == "INSTANTOVERSCHRIJVING" and details_transaction_type_norm.startswith(
            "WEROOVERSCHRIJVING"
        ):
            details_transaction_type = details_transaction_type.replace("OVERSCHRIJVING", "INSTANTOVERSCHRIJVING")
            column_transaction_type = ""
        elif column_transaction_type_norm == "CORRECTIEKAARTVERRICHTING" and (
            details_transaction_type_norm.startswith("STORTINGOPDEREKENINGGEKOPPELDAANDEDEBETKAART")
            or "TERUGBETALINGMETDEBETKAART" in details_transaction_type_norm
            or details_transaction_type_norm.startswith("ANNULERINGBETALING")
        ):
            details_transaction_type = "(Correctie) " + details_transaction_type
            column_transaction_type = ""
        elif (
            column_transaction_type_norm == "KAARTBETALING"
            and details_transaction_type_norm == "BANCONTACTMOBIELEBETALING"
        ):
            details_transaction_type = details_transaction_type.replace("BETALING", "KAARTBETALING")
            column_transaction_type = ""
        elif "KAARTBETALING" in column_transaction_type_norm and (
            "BETALINGMETDEBETKAART" in details_transaction_type_norm
            or "BETALINGMETBANKKAART" in details_transaction_type_norm
        ):
            column_transaction_type = ""
        elif "GELDOPNAME" in column_transaction_type_norm and (
            "GELDOPNAME" in details_transaction_type_norm or "GELDOPNEMING" in details_transaction_type_norm
        ):
            # "Geldopname in buitenland": the column adds where, the details don't say it
            if "BUITENLAND" in column_transaction_type_norm and "BUITENLAND" not in details_transaction_type_norm:
                details_transaction_type = re.sub(
                    r"\b(GELDOPN(?:EMING|AME))\b", r"\1 IN BUITENLAND", details_transaction_type, count=1
                )
            column_transaction_type = ""
        elif (
            column_transaction_type_norm == "INSTANTOVERSCHRIJVING"
            and details_transaction_type_norm == "INSTANTEUROPESEOVERSCHRIJVING"
        ):
            column_transaction_type = ""
        elif (
            column_transaction_type_norm == "DOORLOPENDEBETALINGSOPDRACHT"
            and details_transaction_type_norm == "DOORLOPENDEOPDRACHT"
        ):
            details_transaction_type = ""
        elif (
            column_transaction_type_norm == "TEGENBOEKINGBETAALDEDOMICILIERING"
            and details_transaction_type_norm == "GEWEIGERDEEUROPESEDOMICILIERING"
        ):
            # "betaalde" is implied by "tegenboeking"; "Europese" holds for every domiciliëring since 2014
            column_transaction_type = "Tegenboeking geweigerde domiciliëring" + (
                f" van {details_dom_date}" if details_dom_date else ""
            )
            details_transaction_type = ""
        elif column_transaction_type_norm == "AFLOSSINGKREDIET" and details_transaction_type_norm == "OVERSCHRIJVING":
            details_transaction_type = "Overschrijving voor aflossing krediet"
            column_transaction_type = ""
        elif (
            column_transaction_type_norm == "GRENSOVERSCHRIJDENDEOVERSCHRIJVING"
            and details_transaction_type_norm == "OVERSCHRIJVINGBUITENLAND"
        ):
            column_transaction_type = "Buitenlandse overschrijving"
            details_transaction_type = ""
        elif (
            column_transaction_type_norm == "DRINGENDEOVERSCHRIJVING"
            and details_transaction_type_norm == "DRINGENDEBUITENLANDSEBETALING"
        ):
            column_transaction_type = "Dringende buitenlandse overschrijving"
            details_transaction_type = ""
        elif column_transaction_type_norm == "DIVERSECORRECTIES" and details_transaction_type_norm == "VERBETERING":
            # Same meaning: the column already says it is a correction
            details_transaction_type = ""
        elif (
            column_transaction_type_norm == "REKENINGBEHEER" and "NAAFSLUITINGREKENING" in details_transaction_type_norm
        ):
            # The details say which account management: the transfer after closing the account
            column_transaction_type = ""
        elif re.match(r"\d{4} \d{2}XX XXXX", details_transaction_type):
            # Old transactions: the details hold only the card number
            if column_transaction_type == "Kaartbetaling":
                details_transaction_type = "Betaling met debetkaart " + details_transaction_type
                column_transaction_type = ""
            elif column_transaction_type == "Geldopname met kaart":
                details_transaction_type = "Geldopneming met debetkaart " + details_transaction_type
                column_transaction_type = ""
            elif column_transaction_type == "Geldopname in buitenland":
                details_transaction_type = "Buitenlandse geldopneming met debetkaart " + details_transaction_type
                column_transaction_type = ""
        elif details_transaction_type_norm in column_transaction_type_norm:
            details_transaction_type = ""
        elif column_transaction_type_norm in details_transaction_type_norm:
            column_transaction_type = ""

    if column_transaction_type == "Effecteninschrijving" and "Inschrijving op de Belgische effecten" in (
        details_description
    ):
        column_transaction_type = ""

    if column_transaction_type and details_transaction_type:
        raise ValueError(
            f"Both 'column_transaction_type' ({column_transaction_type}) and "
            f"'details_transaction_type' ({details_transaction_type}) have a value.\n"
            "By now, at least one of them should be empty. A normalisation seems missing..."
        )

    return column_transaction_type, details_transaction_type
