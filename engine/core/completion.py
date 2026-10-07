"""
Completion module:
Handles ALL end-of-processing operations:
- Closing open file handles
- Duplicate index update + backup + atomic commit + rotation
- Moving original CSV
- Moving normalized output
- Cleaning up the temp directory
- Final log + alert (on failure) + exit

This module is the single exit path for the entire processing flow.
"""

import os
import shutil
import sys
import traceback
from typing import Any

from engine.core.duplicate_index import (
    create_updated_duplicate_index,
    rotate_duplicate_backups,
)
from engine.core.runtime import alert


# ---------------------------------------------------------------------------
# Logging + alert + exit
# ---------------------------------------------------------------------------
def log_alert_exit(context: dict[str, Any], exit_code: int, message: str) -> None:
    """Write final log entry, alert on failure (stderr -> cron email), then exit."""

    log_event = context["log_event"]
    logfile_path = context["logfile_path"]
    csv_filename = context["csv_filename"]
    run_timestamp = context["run_timestamp"]

    log_event(logfile_path, message)

    if exit_code != 0:
        subject, _, detail = message.partition("\n")
        alert(subject, f"File: {csv_filename}\nTimestamp: {run_timestamp}\nLog: {logfile_path}\n{detail}".rstrip())

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Atomic copy
# ---------------------------------------------------------------------------
def copy_atomically(source: str, target: str) -> None:
    """Copy via a temp file + rename, so target is never left half-written."""
    temp_target = f"{target}.tmp"
    shutil.copyfile(source, temp_target)
    os.replace(temp_target, target)


# ---------------------------------------------------------------------------
# Close all open writers
# ---------------------------------------------------------------------------
def close_open_writers(context: dict[str, Any]) -> None:
    """Close all file handles associated with CSV writers."""
    for ref in context["open_writers"]:
        f = ref.get("file")
        if f:
            try:
                f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------
def finalize(
    context: dict[str, Any],
    exit_code: int,
    outcome: str,
    message: str,
) -> None:
    """
    Perform all end-of-processing operations.
    This is the single exit path for the entire processing flow.
    """

    paths = context["paths"]
    csv_file_path = context["csv_file_path"]
    csv_filename = context["csv_filename"]
    run_timestamp = context["run_timestamp"]
    logfile_path = context["logfile_path"]

    # Rows that must be added to the duplicate-index for this run.
    # These are prepared in process_csv.py and are independent of normalized output.
    duplicate_index_rows_to_add: list[dict[str, Any]] = context.get("duplicate_index_rows_to_add", [])

    # ----------------------------------------------------------------------
    # 0. Close writers before any file is moved: a file must not move while
    #    open (Windows refuses, other filesystems may truncate). Also covers
    #    the critical exits below, which stop before the end.
    # ----------------------------------------------------------------------
    close_open_writers(context)

    # ----------------------------------------------------------------------
    # 1. Prepare duplicate-index update (critical: without it the rows would
    #    be normalized but never recorded, and reprocessed next time)
    # ----------------------------------------------------------------------
    updated_duplicate_index = None

    try:
        # Only prepare an updated duplicate-index snapshot when there are
        # new rows to add and their normalized output is kept. A crashed run
        # discards its output, so its rows must not be marked as seen.
        if duplicate_index_rows_to_add and outcome in ("success", "partial"):
            updated_duplicate_index, old_columns = create_updated_duplicate_index(
                paths["duplicate_index_csv"],
                paths["duplicate_index_backup_dir"],
                run_timestamp,
                csv_filename,
                duplicate_index_rows_to_add,
            )
            if old_columns is not None:
                new_columns = list(duplicate_index_rows_to_add[0].keys())
                context["log_event"](
                    logfile_path,
                    "Duplicate index columns follow the config now: "
                    f"added {[c for c in new_columns if c not in old_columns]} (old rows: not recorded), "
                    f"removed {[c for c in old_columns if c not in new_columns]}",
                )
    except Exception as e:
        log_alert_exit(
            context,
            97,
            f"DUPLICATE INDEX PREP ERROR: {e}\n\nTraceback:\n{traceback.format_exc()}",
        )

    # ----------------------------------------------------------------------
    # 2. Move original CSV (critical)
    # ----------------------------------------------------------------------
    try:
        if outcome in ("structure_failed", "all_failed", "error"):
            final_csv_path = paths["processed_failed_csv"]
        elif outcome == "partial":
            final_csv_path = paths["processed_partial_csv"]
        else:
            final_csv_path = paths["processed_success_csv"]

        shutil.move(csv_file_path, final_csv_path)

    except Exception as e:
        try:
            shutil.move(csv_file_path, paths["processed_failed_csv"])
        except Exception:
            pass

        log_alert_exit(
            context,
            94,
            f"ORIGINAL CSV MOVE ERROR: {e}\n\nTraceback:\n{traceback.format_exc()}",
        )

    # ----------------------------------------------------------------------
    # 3. Commit duplicate-index (critical)
    # ----------------------------------------------------------------------
    try:
        # Commit only when an updated duplicate-index snapshot was created.
        if duplicate_index_rows_to_add and updated_duplicate_index:
            if os.path.exists(paths["duplicate_index_csv"]):
                shutil.copy2(
                    paths["duplicate_index_csv"],
                    paths["duplicate_index_previous_csv"],
                )
            else:
                open(paths["duplicate_index_previous_csv"], "w", encoding="utf-8").close()

            copy_atomically(updated_duplicate_index, paths["duplicate_index_csv"])

    except Exception as e:
        try:
            shutil.move(final_csv_path, paths["processed_failed_csv"])
        except Exception:
            pass

        log_alert_exit(
            context,
            93,
            f"DUPLICATE INDEX COMMIT ERROR: {e}\n\nTraceback:\n{traceback.format_exc()}",
        )

    # ----------------------------------------------------------------------
    # 4. Move normalized output (critical)
    # ----------------------------------------------------------------------
    try:
        if outcome == "partial":
            normalized_target = paths["normalized_partial_csv"]
        elif outcome == "success":
            normalized_target = paths["normalized_success_csv"]
        else:
            normalized_target = None

        if normalized_target and os.path.exists(paths["temp_normalized_csv"]):
            shutil.move(paths["temp_normalized_csv"], normalized_target)

    except Exception as e:
        if os.path.exists(paths["duplicate_index_previous_csv"]):
            try:
                copy_atomically(paths["duplicate_index_previous_csv"], paths["duplicate_index_csv"])
            except Exception:
                pass

        try:
            shutil.move(final_csv_path, paths["processed_failed_csv"])
        except Exception:
            pass

        log_alert_exit(
            context,
            92,
            f"NORMALIZED OUTPUT MOVE ERROR: {e}\n\nTraceback:\n{traceback.format_exc()}",
        )

    # ----------------------------------------------------------------------
    # 5. Rotate duplicate-index backups (not critical)
    # ----------------------------------------------------------------------
    try:
        rotate_duplicate_backups(
            paths["duplicate_index_backup_dir"],
            context["log_event"],
            logfile_path,
        )
    except Exception:
        pass

    # ----------------------------------------------------------------------
    # 6. Cleanup temp directory
    # ----------------------------------------------------------------------
    try:
        shutil.rmtree(paths["temp_dir"], ignore_errors=True)
    except Exception:
        pass

    # ----------------------------------------------------------------------
    # 7. Final log + alert + exit
    # ----------------------------------------------------------------------
    log_alert_exit(context, exit_code, message)
