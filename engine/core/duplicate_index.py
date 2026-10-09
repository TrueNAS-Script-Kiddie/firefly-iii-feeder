"""
Duplicate index management:
- Load duplicate index
- Append new rows
- Create timestamped backups
- Rotate old backups

This module contains all logic related to maintaining duplicate-index.csv.
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

# Value of a column added to columns.required after a row was indexed: unknown, so
# not compared (an empty value would conflict with every row the bank fills it for)
NOT_RECORDED = "<not recorded>"


# ---------------------------------------------------------------------------
# Load duplicate index
# ---------------------------------------------------------------------------
def load_duplicate_index(duplicate_index_path: str) -> defaultdict[str, list[dict[str, str]]]:
    """
    Load an account's duplicate index from CSV.
    Returns a dict: duplicate_key → list of rows with that key.
    """

    index: defaultdict[str, list[dict[str, str]]] = defaultdict(list)

    if not os.path.exists(duplicate_index_path):
        return index

    with open(duplicate_index_path, newline="", encoding="utf-8") as index_file:
        reader = csv.DictReader(index_file, delimiter=";")

        for row in reader:
            key = (row.get("duplicate_key") or "").strip()
            if key:
                index[key].append(row)

    return index


# ---------------------------------------------------------------------------
# Append new rows to updated_duplicate_index
# ---------------------------------------------------------------------------
def append_to_duplicate_index(duplicate_index_path: str, duplicate_index_rows: list[dict[str, str]]) -> None:
    """
    Append new duplicate-index rows to the updated duplicate-index file.
    Creates the file with header if it does not exist.
    """

    if not duplicate_index_rows:
        return

    file_exists = os.path.exists(duplicate_index_path)
    fieldnames = list(duplicate_index_rows[0].keys())

    with open(duplicate_index_path, "a", newline="", encoding="utf-8") as index_file:
        writer = csv.DictWriter(index_file, fieldnames=fieldnames, delimiter=";")

        if not file_exists:
            writer.writeheader()

        for row in duplicate_index_rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Create updated duplicate-index snapshot
# ---------------------------------------------------------------------------
def create_updated_duplicate_index(
    duplicate_index_path: str,
    backup_dir: str,
    run_id: str,
    duplicate_index_rows: list[dict[str, str]],
) -> tuple[str, list[str] | None]:
    """
    Create a timestamped updated duplicate-index file:
    - Copy existing duplicate-index.csv if present; when columns.required changed,
      rewrite it with the new columns instead (added: NOT_RECORDED, removed: dropped)
    - Otherwise create empty base
    - Append duplicate_index_rows
    Returns the path to the updated duplicate index file, and the old columns when
    they were rewritten (else None).
    """

    # Path for updated snapshot: '<run>-<partition>-duplicate-index.csv'
    partition = os.path.splitext(os.path.basename(duplicate_index_path))[0].replace("-duplicate-index", "")
    updated_duplicate_index = os.path.join(backup_dir, f"{run_id}-{partition}-duplicate-index.csv")

    # Base: existing dup-index or empty file
    old_columns = None
    if os.path.exists(duplicate_index_path):
        columns = list(duplicate_index_rows[0].keys())
        with open(duplicate_index_path, newline="", encoding="utf-8") as index_file:
            reader = csv.DictReader(index_file, delimiter=";")
            existing_columns = reader.fieldnames or []
            if existing_columns == columns:
                shutil.copy2(duplicate_index_path, updated_duplicate_index)
            else:
                old_columns = list(existing_columns)
                with open(updated_duplicate_index, "w", newline="", encoding="utf-8") as updated_file:
                    writer = csv.DictWriter(
                        updated_file, fieldnames=columns, delimiter=";", restval=NOT_RECORDED, extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(reader)

    # Append new rows
    append_to_duplicate_index(updated_duplicate_index, duplicate_index_rows)

    return updated_duplicate_index, old_columns


# ---------------------------------------------------------------------------
# Rotate old backups (by age and count)
# ---------------------------------------------------------------------------
def rotate_duplicate_backups(
    backup_dir: str,
    log_event: Callable[[str, str], None],
    logfile_path: str,
) -> None:
    """
    Rotate old duplicate-index backups by age and count, per account.
    Backup names: <run>-<partition>-duplicate-index.csv, where <run> starts with the run
    timestamp (older backups have the source name in between; the partition is always last).
    The newest backup of an account is always kept.
    Logs only on error. Never interrupts the processing flow.
    """
    suffix = "-duplicate-index.csv"
    try:
        if not os.path.exists(backup_dir):
            return

        backups_per_partition: defaultdict[str, list[tuple[datetime, str]]] = defaultdict(list)

        for filename in os.listdir(backup_dir):
            if not filename.endswith(suffix):
                continue
            try:
                timestamp = datetime.strptime(filename[:15], RUN_TS_FORMAT)
            except ValueError:
                # Ignore files that don't start with a run timestamp
                continue
            partition = filename[: -len(suffix)].rsplit("-", 1)[-1]
            backups_per_partition[partition].append((timestamp, filename))

        cutoff = datetime.now() - timedelta(days=MAX_BACKUP_AGE_DAYS)

        for backups in backups_per_partition.values():
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
# Classify rows against the duplicate index
# ---------------------------------------------------------------------------
def classify_duplicate(
    duplicate_index: dict[str, list[dict[str, Any]]],
    key: str,
    row: dict[str, Any],
    bank_config: dict[str, Any],
) -> str:
    """
    Classify a row against the duplicate index using YAML rules.

    identical = all required fields match (fields NOT_RECORDED in the index are skipped)
    conflict  = key exists but required fields differ
    new       = key not present
    """

    existing_rows = duplicate_index.get(key, [])
    if not existing_rows:
        return "new"

    # Determine which fields must match
    required_fields = list(bank_config["columns"]["required"].keys())

    for existing in existing_rows:
        required_match = True

        for field in required_fields:
            # Missing: a column added to the config since this row was indexed
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
