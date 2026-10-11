"""
Argenta's part of engine.debug_row: the rule of the row's Beschrijving and, for card rows,
every step of the Mededeling parser (what it cut off, what was left) and the parts it found.
"""

from engine.banks.argenta.extract_card_details import extract_card_details
from engine.banks.argenta.normalize import RULES


def show_trace(row: dict) -> None:
    rule = RULES.get(row["transaction_type"])
    if rule is None:
        print(f"-- no rule for Beschrijving {row['transaction_type']!r}: the row fails")
        return
    print(f"-- rule: {rule}")
    if rule.counterparty != "card":
        return

    print("-- card Mededeling parser, step by step")
    trace: list[tuple[str, str, str]] = []
    try:
        found = extract_card_details(row.get("description", ""), trace)
    except Exception as exc:  # noqa: BLE001 - the normalizer fails the row on any exception, so show it
        found, parser_error = None, f"{type(exc).__name__}: {exc}"
    if not trace:
        print("   (no step matched)")
    for step, removed, remaining in trace:
        print(f"   {step:<12} took {removed!r}")
        print(f"   {'':<12} left {remaining!r}")
    if found is None:
        print(f"-- FAILED in the Mededeling parser: {parser_error}")
        return

    print("-- parts found in the Mededeling")
    for key, value in found.items():
        if value:
            print(f"   {key:<14} {value!r}")
