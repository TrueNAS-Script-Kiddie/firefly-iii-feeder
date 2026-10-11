"""
Show how rows of a bank export are read and normalized: every mapped column of
the bank config, why a row is filtered out or invalid, whether the account's row
state already has it (then the normalizer skips it) and its Firefly id, the
bank's own trace when it has one (engine/banks/<bank>/debug_trace.py), and the
normalize_row result or the exact exception.

Usage (on the server, where the row state is; reads only, writes nothing):
  PYTHONPATH=. python3 -m engine.debug_row <file> <row> [<row> ...]

<row> is the source row as the normalizer log names it ("source row 17": the
line in a CSV or the row in an xlsx, the header is 1; "p3/12": page and line of
a PDF), or a value of external_id, e.g. a Fintro Volgnummer like 2019-00135
(also matches filtered rows, such as pending ones).
"""

import importlib
import os
import sys
from collections.abc import Callable

from engine.core.csv_runtime import load_all_bank_configs, load_rows
from engine.core.csv_validation import (
    autodetect_bank,
    extract_duplicate_key,
    filtered_by,
    map_row,
    validate_and_prepare,
)
from engine.core.row_state import FIREFLY_ID, NOT_RECORDED, classify_duplicate, load_row_state, state_filename
from engine.core.runtime import BASE_DIR

ROW_STATE_DIR = os.path.join(BASE_DIR, "data", "row-state")


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
                    print(f"   {'':<26} file  {row.get(field, '')!r}")


def load_trace(bank: str) -> Callable[[dict], None] | None:
    """The bank's show_trace, or None when the bank has no debug_trace module."""
    module_name = f"engine.banks.{bank}.debug_trace"
    try:
        return importlib.import_module(module_name).show_trace
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return None


def show_row(raw_row: dict, validated_row: dict | None, column_map: dict, bank_cfg: dict, states: dict) -> None:
    mapped = map_row(raw_row, column_map)
    for internal_name, column in column_map.items():
        print(f"   {internal_name:<26} {column:<24} {mapped[internal_name]!r}")

    filtered_column = filtered_by(mapped, bank_cfg)
    if filtered_column:
        rule = bank_cfg["columns"]["required"][filtered_column]
        tests = ", ".join(f"{name} {rule[name]!r}" for name in ("filter", "filter_regex") if name in rule)
        print(f"-- filtered out on {filtered_column} {mapped[filtered_column]!r} ({tests}): the normalizer drops it")
        return

    if "_validation_error" in validated_row:
        print(f"-- INVALID, fails before normalize_row: {validated_row['_validation_error']}")
        return

    show_duplicate_status(validated_row, bank_cfg, states)

    show_trace = load_trace(bank_cfg["bank"])
    if show_trace:
        show_trace(validated_row)

    print("-- result")
    try:
        normalized = importlib.import_module(f"engine.banks.{bank_cfg['bank']}").normalize_row(validated_row)
    except Exception as exc:  # noqa: BLE001 - the normalizer fails the row on any exception, so show it
        print(f"   FAILED: {type(exc).__name__}: {exc}")
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
    name = os.path.basename(path)

    try:
        bank_configs = load_all_bank_configs(os.path.join(BASE_DIR, "config"))
        raw_rows, source_lines, _ = load_rows(path, bank_configs)
        bank_cfg = autodetect_bank(raw_rows, bank_configs)
        validated_rows, column_map, _ = validate_and_prepare(raw_rows, bank_cfg, source_lines)
    except ValueError as exc:
        print(f"{name}: {exc}")
        return 1
    print(f"{name}: bank {bank_cfg['bank']}, {len(raw_rows)} rows\n")

    validated_by_line = {row["_source_line"]: row for row in validated_rows}
    external_id_column = column_map.get("external_id")
    states: dict = {}  # row-state path -> loaded state, read once

    selected: set[int] = set()
    for arg in sys.argv[2:]:
        found = [i for i, line in enumerate(source_lines) if line == arg]
        if external_id_column:
            found += [i for i, raw in enumerate(raw_rows) if raw.get(external_id_column, "").strip() == arg]
        if not found:
            print(f"== {arg!r}: no such source row or external_id ({external_id_column or 'none'}) in {name}\n")
        selected.update(found)

    for i in sorted(selected):
        print(f"== source row {source_lines[i]} of {name}")
        show_row(raw_rows[i], validated_by_line.get(source_lines[i]), column_map, bank_cfg, states)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
