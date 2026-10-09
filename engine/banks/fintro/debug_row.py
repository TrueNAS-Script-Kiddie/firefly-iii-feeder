"""
Show how Fintro rows are taken apart: the mapped columns, whether the
account's duplicate index already has the row (then the normalizer skips it),
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
from engine.core.duplicate_index import NOT_RECORDED, classify_duplicate, load_duplicate_index
from engine.core.runtime import BASE_DIR

DUPLICATE_INDEX_DIR = os.path.join(BASE_DIR, "data", "duplicate-index")

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


def show_duplicate_status(row: dict, bank_cfg: dict, indexes: dict) -> None:
    """The normalizer's duplicate check for this row, as process_csv does it."""
    partition_by = bank_cfg.get("duplicate_key", {}).get("partition_by")
    partition = row.get(partition_by, "").replace(" ", "").upper() if partition_by else bank_cfg["bank"]
    index_path = os.path.join(DUPLICATE_INDEX_DIR, f"{partition}-duplicate-index.csv")
    print("-- duplicate index")
    if not os.path.exists(index_path):
        print(f"   no index at {index_path}: the row counts as new")
        return
    if index_path not in indexes:
        indexes[index_path] = load_duplicate_index(index_path)
    key = extract_duplicate_key(row, bank_cfg)
    status = classify_duplicate(indexes[index_path], key, row, bank_cfg) if key else "no key"
    if status == "new":
        print(f"   new: key {key} is not in the index")
    elif status == "identical":
        print(f"   identical: key {key} is in the index with the same values -> the normalizer skips it")
    elif status == "no key":
        print("   no duplicate key could be extracted -> the normalizer fails the row (duplicate-failed)")
    else:
        print(f"   conflict: key {key} is in the index with other values -> the normalizer fails it (duplicate-failed)")
        for existing in indexes[index_path][key]:
            for field in bank_cfg["columns"]["required"]:
                indexed = existing.get(field, NOT_RECORDED)
                if indexed != NOT_RECORDED and indexed.strip() != row.get(field, "").strip():
                    print(f"   {field:<26} index {indexed!r}")
                    print(f"   {'':<26} csv   {row.get(field, '')!r}")


def show_row(row: dict, bank_cfg: dict, indexes: dict) -> None:
    for field in SHOWN_COLUMNS:
        print(f"   {field:<26} {row.get(field, '')!r}")

    if "_validation_error" in row:
        print(f"-- FAILS before parsing: {row['_validation_error']}")
        return

    show_duplicate_status(row, bank_cfg, indexes)

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
    indexes: dict = {}  # index path -> loaded index, read once

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
            show_row(row, bank_cfg, indexes)
        elif 2 <= line <= len(raw_rows) + 1:
            raw = raw_rows[line - 2]
            print(f"   filtered out (Volgnummer {raw.get('Volgnummer')!r}, Status {raw.get('Status')!r})")
        else:
            print(f"   no such line (the file has lines 2..{len(raw_rows) + 1})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
