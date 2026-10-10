"""
Fintro's part of engine.debug_row: every step of the details parser (what it
cut off, what was left) and the values it found.
"""

from engine.banks.fintro.extract_details import extract_details
from engine.banks.fintro.parsers import parse_ddmmyyyy


def show_trace(row: dict) -> None:
    print("-- details parser, step by step")
    trace: list[tuple[str, str, str]] = []
    try:
        found = extract_details(row.get("details", ""), parse_ddmmyyyy(row["primary_transaction_date"]), trace)
    except Exception as exc:  # noqa: BLE001 - the normalizer fails the row on any exception, so show it
        found, parser_error = None, f"{type(exc).__name__}: {exc}"
    if not trace:
        print("   (no step matched)")
    for step, removed, remaining in trace:
        print(f"   {step:<22} took {removed!r}")
        print(f"   {'':<22} left {remaining!r}")
    if found is None:
        print(f"-- FAILED in the details parser: {parser_error}")
        return

    print("-- values found in details")
    for key, value in found.items():
        if value:
            print(f"   {key:<32} {value!r}")
