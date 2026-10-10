"""
Row state: per account, every bank row the feeder already knows and the Firefly transaction it
became. One file per account, data/row-state/<account>.csv:
- duplicate_key: the row's key within the account (row_key = <account>|<duplicate_key>)
- firefly_id: the Firefly transaction the row created or claimed; empty while it waits in
  data/normalized/
- the bank's own values of columns.required, to detect a row the bank rewrote

The normalizer adds rows (finalize); the importer fills firefly_id and drops the rows Firefly
refused, so a later export carrying them retries them.
"""

import csv
import os
import shutil
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Backup + rotation configuration
# ---------------------------------------------------------------------------
MAX_BACKUPS = 50
MAX_BACKUP_AGE_DAYS = 365
RUN_TS_FORMAT = "%Y%m%d-%H%M%S"

FIREFLY_ID = "firefly_id"

# Value of a column added to columns.required after a row was recorded: unknown, so
# not compared (an empty value would conflict with every row the bank fills it for)
NOT_RECORDED = "<not recorded>"


def state_filename(account: str) -> str:
    """The account's file name: the account is the first part of row_key."""
    return f"{account}.csv"


# ---------------------------------------------------------------------------
# Load row state
# ---------------------------------------------------------------------------
def load_row_state(row_state_path: str) -> defaultdict[str, list[dict[str, str]]]:
    """
    Load an account's row state from CSV.
    Returns a dict: duplicate_key → list of rows with that key.
    """

    state: defaultdict[str, list[dict[str, str]]] = defaultdict(list)

    if not os.path.exists(row_state_path):
        return state

    with open(row_state_path, newline="", encoding="utf-8") as state_file:
        reader = csv.DictReader(state_file, delimiter=";")

        for row in reader:
            key = (row.get("duplicate_key") or "").strip()
            if key:
                state[key].append(row)

    return state


# ---------------------------------------------------------------------------
# Append new rows to an updated row state
# ---------------------------------------------------------------------------
def append_to_row_state(row_state_path: str, rows: list[dict[str, str]]) -> None:
    """
    Append new rows to the updated row-state file.
    Creates the file with header if it does not exist.
    """

    if not rows:
        return

    file_exists = os.path.exists(row_state_path)
    fieldnames = list(rows[0].keys())

    with open(row_state_path, "a", newline="", encoding="utf-8") as state_file:
        writer = csv.DictWriter(state_file, fieldnames=fieldnames, delimiter=";")

        if not file_exists:
            writer.writeheader()

        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Create updated row state (normalizer)
# ---------------------------------------------------------------------------
def create_updated_row_state(
    row_state_path: str,
    work_dir: str,
    run_id: str,
    rows: list[dict[str, str]],
) -> tuple[str, list[str] | None]:
    """
    Write the account's state plus the new rows to '<run>-<account>.csv' in work_dir:
    - Copy the current state if present; when columns.required changed,
      rewrite it with the new columns instead (added: NOT_RECORDED, removed: dropped)
    - Append rows
    Returns the path of that file, and the old columns when they were rewritten (else None).
    The live state is not touched: finalize replaces it once the normalized output is in place.
    """

    account = os.path.splitext(os.path.basename(row_state_path))[0]
    updated_row_state = os.path.join(work_dir, f"{run_id}-{account}.csv")

    old_columns = None
    if os.path.exists(row_state_path):
        columns = list(rows[0].keys())
        with open(row_state_path, newline="", encoding="utf-8") as state_file:
            reader = csv.DictReader(state_file, delimiter=";")
            existing_columns = reader.fieldnames or []
            if existing_columns == columns:
                shutil.copy2(row_state_path, updated_row_state)
            else:
                old_columns = list(existing_columns)
                with open(updated_row_state, "w", newline="", encoding="utf-8") as updated_file:
                    writer = csv.DictWriter(
                        updated_file, fieldnames=columns, delimiter=";", restval=NOT_RECORDED, extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(reader)

    append_to_row_state(updated_row_state, rows)

    return updated_row_state, old_columns


# ---------------------------------------------------------------------------
# The importer's side
# ---------------------------------------------------------------------------
def load_firefly_ids(row_state_dir: str) -> dict[str, str]:
    """row_key -> firefly_id ('' while not imported) for every row of every account."""
    ids: dict[str, str] = {}
    if not os.path.isdir(row_state_dir):
        return ids
    for name in sorted(os.listdir(row_state_dir)):
        path = os.path.join(row_state_dir, name)
        if not name.endswith(".csv") or not os.path.isfile(path):
            continue
        account = os.path.splitext(name)[0]
        with open(path, newline="", encoding="utf-8") as state_file:
            for row in csv.DictReader(state_file, delimiter=";"):
                firefly_id = row.get(FIREFLY_ID) or ""
                ids[f"{account}|{row.get('duplicate_key', '')}"] = "" if firefly_id == NOT_RECORDED else firefly_id
    return ids


def record_import(row_state_dir: str, outcomes: dict[str, str | None]) -> list[str]:
    """
    Write the importer's outcome per row_key: the Firefly id the row created or claimed, or None
    for a row Firefly refused, which is dropped so a later export carrying it retries it. Each
    account's file is replaced in one step. Returns the row_keys not found in the state.
    """
    per_account: defaultdict[str, dict[str, str | None]] = defaultdict(dict)
    for row_key, firefly_id in outcomes.items():
        account, _, key = row_key.partition("|")
        per_account[account][key] = firefly_id

    missing: list[str] = []
    for account, by_key in per_account.items():
        path = os.path.join(row_state_dir, state_filename(account))
        if not os.path.exists(path):
            missing.extend(f"{account}|{key}" for key in by_key)
            continue
        with open(path, newline="", encoding="utf-8") as state_file:
            reader = csv.DictReader(state_file, delimiter=";")
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if FIREFLY_ID not in fieldnames:
            fieldnames.insert(1, FIREFLY_ID)
        found = set()
        kept = []
        for row in rows:
            key = row.get("duplicate_key", "")
            if key in by_key:
                found.add(key)
                if by_key[key] is None:
                    continue
                row[FIREFLY_ID] = by_key[key]
            kept.append(row)
        missing.extend(f"{account}|{key}" for key in by_key if key not in found)

        temp_path = f"{path}.tmp"
        with open(temp_path, "w", newline="", encoding="utf-8") as updated_file:
            writer = csv.DictWriter(updated_file, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            writer.writerows(kept)
        os.replace(temp_path, path)
    return missing


# ---------------------------------------------------------------------------
# Rotate old backups (by age and count)
# ---------------------------------------------------------------------------
def rotate_backups(
    backup_dir: str,
    log_event: Callable[[str, str], None],
    logfile_path: str,
) -> None:
    """
    Rotate old row-state backups by age and count, per account.
    Backup names: <run>-<account>.csv, where <run> starts with the run timestamp.
    The newest backup of an account is always kept.
    Logs only on error. Never interrupts the processing flow.
    """
    try:
        if not os.path.exists(backup_dir):
            return

        backups_per_account: defaultdict[str, list[tuple[datetime, str]]] = defaultdict(list)

        for filename in os.listdir(backup_dir):
            if not filename.endswith(".csv"):
                continue
            try:
                timestamp = datetime.strptime(filename[:15], RUN_TS_FORMAT)
            except ValueError:
                # Ignore files that don't start with a run timestamp
                continue
            account = os.path.splitext(filename)[0].rsplit("-", 1)[-1]
            backups_per_account[account].append((timestamp, filename))

        cutoff = datetime.now() - timedelta(days=MAX_BACKUP_AGE_DAYS)

        for backups in backups_per_account.values():
            # Newest first; index 0 is always kept
            backups.sort(reverse=True)
            for position, (timestamp, filename) in enumerate(backups):
                if position == 0 or (position < MAX_BACKUPS and timestamp >= cutoff):
                    continue
                try:
                    os.remove(os.path.join(backup_dir, filename))
                except Exception as exc:
                    log_event(logfile_path, f"[ROTATION ERROR] {exc}")

    except Exception as exc:
        log_event(logfile_path, f"[ROTATION ERROR] {exc}")


# ---------------------------------------------------------------------------
# Classify rows against the row state
# ---------------------------------------------------------------------------
def classify_duplicate(
    row_state: dict[str, list[dict[str, Any]]],
    key: str,
    row: dict[str, Any],
    bank_config: dict[str, Any],
) -> str:
    """
    Classify a row against the row state using YAML rules.

    identical = all required fields match (fields NOT_RECORDED in the state are skipped)
    conflict  = key exists but required fields differ
    new       = key not present
    """

    existing_rows = row_state.get(key, [])
    if not existing_rows:
        return "new"

    # Determine which fields must match
    required_fields = list(bank_config["columns"]["required"].keys())

    for existing in existing_rows:
        required_match = True

        for field in required_fields:
            # Missing: a column added to the config since this row was recorded
            existing_value = existing.get(field, NOT_RECORDED)
            if existing_value == NOT_RECORDED:
                continue
            if existing_value.strip() != row.get(field, "").strip():
                required_match = False
                break

        if required_match:
            return "identical"

    # Key exists, but no required-field match
    return "conflict"
