"""
Show how Fintro rows are taken apart: the mapped columns, whether the
account's row state already has the row (then the normalizer skips it) and its Firefly id,
every step of the details parser (what it cut off, what was left), the values
it found, and the final result or the exact reason the row fails.

Usage (on the server, where the CSVs are; reads only, writes nothing):
  PYTHONPATH=. python3 -m engine.banks.fintro.debug_row <csv> <row> [<row> ...]

<row> is either a line number in the CSV (the header is line 1), as in the
normalizer log ("Normalize failed on source row <line>: ..."), or a Volgnummer
like 2019-00135 (also matches filtered rows, e.g. pending ones: 2026-).
"""

import os
import re
import sys

import engine.banks.fintro as fintro
from engine.banks.fintro.extract_details import extract_details
from engine.banks.fintro.parsers import parse_ddmmyyyy
from engine.core.csv_runtime import load_all_bank_configs, load_csv_rows
from engine.core.csv_validation import autodetect_bank, extract_duplicate_key, validate_and_prepare
from engine.core.row_state import FIREFLY_ID, NOT_RECORDED, classify_duplicate, load_row_state, state_filename
from engine.core.runtime import BASE_DIR

ROW_STATE_DIR = os.path.join(BASE_DIR, "data", "row-state")

SHOWN_COLUMNS = [
    "external_id",
    "primary_transaction_date",
    "interest_date",
    "amount",
    "transaction_type",
    "opposing_account_iban",
    "opposing_account_name",
    "description",
    "details",
]


def show_duplicate_status(row: dict, bank_cfg: dict, states: dict) -> None:
    """The normalizer's duplicate check for this row, as process_csv does it, and its Firefly id."""
    partition_by = bank_cfg.get("duplicate_key", {}).get("partition_by")
    partition = row.get(partition_by, "").replace(" ", "").upper() if partition_by else bank_cfg["bank"]
    state_path = os.path.join(ROW_STATE_DIR, state_filename(partition))
    print("-- row state")
    if not os.path.exists(state_path):
        print(f"   no row state at {state_path}: the row counts as new")
        return
    if state_path not in states:
        states[state_path] = load_row_state(state_path)
    key = extract_duplicate_key(row, bank_cfg)
    status = classify_duplicate(states[state_path], key, row, bank_cfg) if key else "no key"
    if status == "new":
        print(f"   new: key {key} is not in the row state")
    elif status == "identical":
        firefly_id = states[state_path][key][0].get(FIREFLY_ID) or "none yet (waits in data/normalized/)"
        print(f"   identical: key {key} is in the row state with the same values -> the normalizer skips it")
        print(f"   Firefly transaction: {firefly_id}")
    elif status == "no key":
        print("   no duplicate key could be extracted -> the normalizer fails the row (duplicate-failed)")
    else:
        print(f"   conflict: key {key} is in the row state with other values -> the normalizer fails it")
        for existing in states[state_path][key]:
            for field in bank_cfg["columns"]["required"]:
                recorded = existing.get(field, NOT_RECORDED)
                if recorded != NOT_RECORDED and recorded.strip() != row.get(field, "").strip():
                    print(f"   {field:<26} state {recorded!r}")
                    print(f"   {'':<26} csv   {row.get(field, '')!r}")


def show_row(row: dict, bank_cfg: dict, states: dict) -> None:
    for field in SHOWN_COLUMNS:
        print(f"   {field:<26} {row.get(field, '')!r}")

    if "_validation_error" in row:
        print(f"-- FAILS before parsing: {row['_validation_error']}")
        return

    show_duplicate_status(row, bank_cfg, states)

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

    print("-- result")
    try:
        normalized = fintro.normalize_row(row)
    except Exception as exc:  # noqa: BLE001 - as above
        print(f"   FAILED after parsing: {type(exc).__name__}: {exc}")
        return
    print("   OK")
    for key, value in normalized.items():
        if value:
            print(f"   {key:<40} {value!r}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = sys.argv[1]

    raw_rows = load_csv_rows(path)
    bank_cfg = autodetect_bank(raw_rows, load_all_bank_configs(os.path.join(BASE_DIR, "config")))
    validated_rows, column_map, _ = validate_and_prepare(raw_rows, bank_cfg)
    by_line = {row["_source_line"]: row for row in validated_rows}
    states: dict = {}  # row-state path -> loaded state, read once

    # Arguments: line numbers, or Volgnummers resolved to their line(s) among all raw rows
    lines: set[int] = set()
    for arg in sys.argv[2:]:
        if arg.isdigit():
            lines.add(int(arg))
        elif re.fullmatch(r"\d{4}-\d*", arg):
            found = [i + 2 for i, raw in enumerate(raw_rows) if raw.get(column_map["external_id"], "").strip() == arg]
            if not found:
                print(f"== Volgnummer {arg}: not in {os.path.basename(path)}\n")
            lines.update(found)
        else:
            print(f"== {arg!r}: neither a line number nor a Volgnummer (YYYY-NNNNN)\n")

    for line in sorted(lines):
        print(f"== line {line} of {os.path.basename(path)}")
        row = by_line.get(line)
        if row is not None:
            show_row(row, bank_cfg, states)
        elif 2 <= line <= len(raw_rows) + 1:
            raw = raw_rows[line - 2]
            print(f"   filtered out (Volgnummer {raw.get('Volgnummer')!r}, Status {raw.get('Status')!r})")
        else:
            print(f"   no such line (the file has lines 2..{len(raw_rows) + 1})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
