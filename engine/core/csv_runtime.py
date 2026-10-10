"""
CSV runtime helpers:
- CSV loading (encoding + delimiter detection)
- Writer creation
- Failed-row writing
- Bank config loading
- Path construction for a single pipeline run

This module contains all CSV-related runtime infrastructure.
Nothing more, nothing less.
"""

import codecs
import csv
import os
import re
from typing import Any

import yaml

# A name starting with a run id is a copy from the archive (start-over): it keeps the run id
# of its first arrival, so a reload processes the files in their original order
ARRIVAL_RUN_ID = re.compile(r"^([0-9]{8}-[0-9]{6}-[0-9]{3})-")


# ---------------------------------------------------------------------------
# Prepare paths
# ---------------------------------------------------------------------------
def build_paths(data_dir: str, archive_dir: str, run_id: str, source_filename: str) -> dict[str, str]:
    """
    Construct all directory and file paths for a single pipeline run.

    Output names start as '<run>-unknown-<source name>': before the bank is known
    the source name is the only clue. describe_output() replaces them once bank,
    account and period are known; duplicate_index_csv and the archive folder are set then too.
    An original that is not finished goes to archive/unprocessed/ as '<run>-<source name>',
    a copy from the archive under its own name.
    """
    stem = os.path.splitext(source_filename)[0]
    unprocessed_name = source_filename if ARRIVAL_RUN_ID.match(source_filename) else f"{run_id}-{source_filename}"

    # fmt: off
    paths = {
        # Directories
        "failed_dir": os.path.join(data_dir, "failed-rows"),
        "normalized_dir": os.path.join(data_dir, "normalized"),
        "temp_dir": os.path.join(data_dir, "temp"),
        "duplicate_index_dir": os.path.join(data_dir, "duplicate-index"),
        "duplicate_index_backup_dir": os.path.join(data_dir, "duplicate-index", "backups"),

        # Duplicate index (placeholder, overwritten later)
        "duplicate_index_csv": os.path.join(data_dir, "duplicate-index", "UNSET.csv"),
        "duplicate_index_previous_csv": os.path.join(data_dir, "temp", "previous-duplicate-index.csv"),

        # Temporary normalized output
        "temp_normalized_csv": os.path.join(data_dir, "temp", f"{run_id}.tmp.csv"),

        # Archive; the folder for a finished original is known only with bank and account
        "archive_unprocessed_dir": os.path.join(archive_dir, "unprocessed"),
        "archive_unprocessed": os.path.join(archive_dir, "unprocessed", unprocessed_name),
        "archive_account_dir": "",
        "archive_original_stem": "",
    }
    # fmt: on
    paths.update(output_paths(data_dir, f"{run_id}-unknown-{stem}"))
    return paths


def output_paths(data_dir: str, base: str) -> dict[str, str]:
    """Every file in data/ named after one bank file: '<base>-<stage>.csv'."""
    return {
        "output_base": base,
        # Failed rows
        "failed_normalize_csv": os.path.join(data_dir, "failed-rows", f"{base}-normalize-failed.csv"),
        "failed_duplicate_csv": os.path.join(data_dir, "failed-rows", f"{base}-duplicate-failed.csv"),
        # Normalized output
        "normalized_partial_csv": os.path.join(data_dir, "normalized", f"{base}-normalized-partial.csv"),
        "normalized_success_csv": os.path.join(data_dir, "normalized", f"{base}-normalized.csv"),
    }


def archive_paths(
    archive_dir: str, run_id: str, source_filename: str, bank: str, account: str, first_date: str, last_date: str
) -> dict[str, str]:
    """
    Folder and name of a finished original: archive/originals/<bank>/<account>/
    '<arrival run>-<first>_<last>' (period left out when unknown); finalize adds '-<sha8><ext>'.
    """
    arrival = ARRIVAL_RUN_ID.match(source_filename)
    stem = arrival.group(1) if arrival else run_id
    if first_date:
        stem = f"{stem}-{first_date}_{last_date}"
    return {
        "archive_account_dir": os.path.join(archive_dir, "originals", bank, account),
        "archive_original_stem": stem,
    }


def describe_output(run_id: str, bank: str, account: str, first_date: str, last_date: str) -> str:
    """'<run>-<bank>-<account>-<first>_<last>'; parts that are unknown are left out."""
    parts = [run_id, bank]
    if account:
        parts.append(account)
    if first_date:
        parts.append(f"{first_date}_{last_date}")
    return "-".join(parts)


# ---------------------------------------------------------------------------
# Load CSV
# ---------------------------------------------------------------------------
class SemicolonDialect(csv.excel):
    """Fallback dialect; a subclass, so the shared csv.excel stays untouched."""

    delimiter = ";"


def load_csv_rows(csv_file_path: str) -> list[dict[str, str]]:
    """
    Load CSV rows into a list of dictionaries.

    Encoding strategy:
    - UTF-16 (Excel "Unicode Text") when the file starts with its byte-order mark;
      cp1252 decodes almost any bytes, so it cannot be a fallback after cp1252
    - Otherwise UTF-8 first (most common)
    - Fallback to Windows-1252 (most common non-UTF-8 in BE/NL)

    Delimiter strategy:
    - Auto-detect via csv.Sniffer()
    - Fallback to semicolon

    Returns:
        List of dicts with raw column names.
    """

    # ------------------------------------------------------------
    # 1. Try reading file with different encodings
    # ------------------------------------------------------------
    with open(csv_file_path, "rb") as f:
        starts_with_utf16_bom = f.read(2) in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
    encodings_to_try = ["utf-16"] if starts_with_utf16_bom else ["utf-8-sig", "cp1252"]

    file_text = None
    used_encoding = None

    for enc in encodings_to_try:
        try:
            with open(csv_file_path, encoding=enc) as f:
                file_text = f.read()
            used_encoding = enc
            break
        except UnicodeDecodeError:
            continue

    if file_text is None:
        raise ValueError(f"Unable to decode CSV file with {', '.join(encodings_to_try)}.")

    # ------------------------------------------------------------
    # 2. Detect delimiter
    # ------------------------------------------------------------
    try:
        detected_dialect = csv.Sniffer().sniff(file_text[:4096])
    except csv.Error:
        detected_dialect = SemicolonDialect

    # ------------------------------------------------------------
    # 3. Parse CSV using detected encoding + dialect
    # ------------------------------------------------------------
    rows: list[dict[str, str]] = []

    with open(csv_file_path, encoding=used_encoding, newline="") as f:
        reader = csv.DictReader(f, dialect=detected_dialect)
        for row in reader:
            cleaned_row = {k: (v if v is not None else "") for k, v in row.items()}
            rows.append(cleaned_row)

    return rows


# ---------------------------------------------------------------------------
# Writer creation
# ---------------------------------------------------------------------------
def ensure_writer(path: str, writer_ref: dict[str, Any], fieldnames: list[str]) -> csv.DictWriter:
    """Lazily create a CSV writer and file handle. Headers written once."""
    if writer_ref.get("writer") is None:
        f = open(path, "w", newline="", encoding="utf-8")
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        w.writeheader()
        writer_ref["writer"] = w
        writer_ref["file"] = f

    return writer_ref["writer"]


# ---------------------------------------------------------------------------
# Write a failed row
# ---------------------------------------------------------------------------
def write_failed_row(path: str, writer_ref: dict[str, Any], row: dict[str, Any]) -> None:
    """Write a failed row to the given CSV file."""
    writer = ensure_writer(path, writer_ref, list(row.keys()))
    writer.writerow(row)


# ---------------------------------------------------------------------------
# Load all bank configs
# ---------------------------------------------------------------------------
def load_all_bank_configs(config_dir: str) -> dict[str, dict[str, Any]]:
    """
    Load all YAML bank configuration files from the given directory.
    Returns a dict: bank_name -> config_dict
    """

    configs: dict[str, dict[str, Any]] = {}

    for filename in os.listdir(config_dir):
        if not filename.endswith(".yaml"):
            continue

        full_path = os.path.join(config_dir, filename)

        with open(full_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        bank_name = os.path.splitext(filename)[0]
        cfg["bank"] = bank_name

        configs[bank_name] = cfg

    return configs
