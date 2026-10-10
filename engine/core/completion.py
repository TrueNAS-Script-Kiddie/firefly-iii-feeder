"""
Completion module:
Handles ALL end-of-processing operations:
- Closing open file handles
- Duplicate index update + backup + atomic commit + rotation
- Archiving the original (archive/originals/ or archive/unprocessed/)
- Moving normalized output
- Cleaning up the temp directory
- Final log + alert (on failure) + exit

This module is the single exit path for the entire processing flow.
"""

import hashlib
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

# Python finished the file, however many rows succeeded: the original is real bank data for
# archive/originals/. Any other outcome (rejected, crashed) goes to archive/unprocessed/.
FINISHED_OUTCOMES = ("success", "partial", "all_failed", "all_full_duplicates", "all_filtered")


# ---------------------------------------------------------------------------
# Logging + alert + exit
# ---------------------------------------------------------------------------
def log_alert_exit(context: dict[str, Any], exit_code: int, message: str) -> None:
    """Write final log entry, alert on failure (stderr -> cron email), then exit."""

    log_event = context["log_event"]
    logfile_path = context["logfile_path"]
    csv_filename = context["csv_filename"]
    run_id = context["run_id"]

    log_event(logfile_path, message)

    if exit_code != 0:
        subject, _, detail = message.partition("\n")
        lines = [f"File: {csv_filename}", f"Run: {run_id}", f"Log: {logfile_path}"]
        if context.get("original_archived_as"):
            lines.append(f"Original: {context['original_archived_as']}")
        alert(subject, "\n".join([*lines, detail]).rstrip())

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def archived_copies(folder: str, sha256: str, name_suffix: str | None = None) -> list[str]:
    """
    Files directly in folder with this content, or (with name_suffix) whose name ends in it:
    '-<sha8><ext>' names the export as it arrived, also after a hand fix changed its content.
    """
    if not os.path.isdir(folder):
        return []
    copies = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and ((name_suffix and name.endswith(name_suffix)) or file_sha256(path) == sha256):
            copies.append(path)
    return copies


def move_to_unprocessed(context: dict[str, Any], path: str | None) -> None:
    """
    Compensating move after a critical failure: the original's rows were not recorded, so it
    must be dropped in again. Nothing to move when it was dropped as already archived; a copy
    whose content unprocessed/ already holds is dropped too.
    """
    target = context["paths"]["archive_unprocessed"]
    try:
        if path and os.path.exists(path):
            existing = archived_copies(context["paths"]["archive_unprocessed_dir"], file_sha256(path))
            if existing:
                os.remove(path)
                context["original_archived_as"] = f"{existing[0]} (already archived, copy dropped)"
                return
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(path, target)
            context["original_archived_as"] = target
    except Exception:
        pass


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
    run_id = context["run_id"]

    # Give the log the name of the other outputs; bash created it before the bank was
    # known. Not critical: on failure it keeps its first name.
    named_log = os.path.join(os.path.dirname(context["logfile_path"]), f"{paths['output_base']}.log")
    try:
        os.replace(context["logfile_path"], named_log)
        context["logfile_path"] = named_log
    except OSError:
        pass
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
                run_id,
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
    # 2. Archive the original (critical). Finished → archive/originals/
    #    <bank>/<account>/<arrival run>-<first>_<last>-<sha8>.<ext>, else →
    #    archive/unprocessed/. A copy whose content is already in that folder
    #    is dropped; in originals/ also one whose sha8 a name there carries.
    # ----------------------------------------------------------------------
    finished = outcome in FINISHED_OUTCOMES
    archived_original = None  # where the original went; None when dropped
    try:
        sha256 = file_sha256(csv_file_path)
        if finished:
            folder = paths["archive_account_dir"]
            name_suffix = f"-{sha256[:8]}{os.path.splitext(context['csv_filename'])[1]}"
            target = os.path.join(folder, f"{paths['archive_original_stem']}{name_suffix}")
        else:
            folder = paths["archive_unprocessed_dir"]
            name_suffix = None
            target = paths["archive_unprocessed"]
        os.makedirs(folder, exist_ok=True)

        existing = archived_copies(folder, sha256, name_suffix)
        if existing:
            os.remove(csv_file_path)
            context["original_archived_as"] = f"{existing[0]} (already archived, copy dropped)"
        else:
            # An existing file of that name in unprocessed/ is the same export: replaced
            shutil.move(csv_file_path, target)
            archived_original = target
            context["original_archived_as"] = target
        context["log_event"](logfile_path, f"Original: {context['original_archived_as']}")

    except Exception as e:
        move_to_unprocessed(context, csv_file_path)
        log_alert_exit(
            context,
            94,
            f"ORIGINAL NOT ARCHIVED: {e}\n\nTraceback:\n{traceback.format_exc()}",
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
        move_to_unprocessed(context, archived_original)

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

        move_to_unprocessed(context, archived_original)

        log_alert_exit(
            context,
            92,
            f"NORMALIZED OUTPUT MOVE ERROR: {e}\n\nTraceback:\n{traceback.format_exc()}",
        )

    # ----------------------------------------------------------------------
    # 5. A finished original no longer belongs in archive/unprocessed/ (a
    #    rejected file that now succeeds): remove copies with the same
    #    content (not critical)
    # ----------------------------------------------------------------------
    if finished:
        try:
            for path in archived_copies(paths["archive_unprocessed_dir"], sha256):
                os.remove(path)
                context["log_event"](logfile_path, f"Removed from unprocessed: {path}")
        except Exception as exc:
            context["log_event"](logfile_path, f"[UNPROCESSED CLEANUP ERROR] {exc}")

    # ----------------------------------------------------------------------
    # 6. Rotate duplicate-index backups (not critical)
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
    # 7. Cleanup temp directory
    # ----------------------------------------------------------------------
    try:
        shutil.rmtree(paths["temp_dir"], ignore_errors=True)
    except Exception:
        pass

    # ----------------------------------------------------------------------
    # 8. Final log + alert + exit
    # ----------------------------------------------------------------------
    log_alert_exit(context, exit_code, message)
