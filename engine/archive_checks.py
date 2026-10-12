"""
Checks across the archived originals, for what no single export shows (e.g. card statements
against the account that pays them). Every engine/banks/<bank>/archive_check.py has
check_archive(archive_dir) -> list[str], one plain text per difference; this runs them all.

firefly-iii-feeder.bash runs it after the importer, in a run that processed an export and left
none in data/incoming/, so exports are never compared half loaded. Every difference is an alert on
stderr (exit 1); a check that crashes is alerted as such, and the others still run.

Usage (reads only):
  PYTHONPATH=. python3 -m engine.archive_checks [--print] [--archive <dir>]
"""

import argparse
import importlib
import os
import sys
import traceback

from engine.core.runtime import BASE_DIR, alert

BANKS_DIR = os.path.join(BASE_DIR, "engine", "banks")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")


def find_checks() -> list[str]:
    """Every bank with an archive_check module."""
    return sorted(
        bank for bank in os.listdir(BANKS_DIR) if os.path.isfile(os.path.join(BANKS_DIR, bank, "archive_check.py"))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print to stdout instead of alerting; exit 0")
    parser.add_argument("--archive", default=ARCHIVE_DIR, help=f"archive folder (default {ARCHIVE_DIR})")
    args = parser.parse_args()

    def report(subject: str, body: str) -> None:
        if args.print:
            print(f"{subject}\n{body}\n")
        else:
            alert(
                subject,
                f"{body}\n\nAlerted at every run that processes an export, while it stays. By hand: "
                "PYTHONPATH=. python3 -m engine.archive_checks --print",
            )

    found = False
    for bank in find_checks():
        try:
            differences = importlib.import_module(f"engine.banks.{bank}.archive_check").check_archive(args.archive)
        except Exception:
            report(f"ARCHIVE CHECK CRASHED: {bank}", traceback.format_exc())
            found = True
            continue
        if differences:
            report(f"ARCHIVE CHECK: {len(differences)} DIFFERENCES ({bank})", "\n".join(differences))
            found = True
        elif args.print:
            print(f"{bank}: no differences")
    return 1 if found and not args.print else 0


if __name__ == "__main__":
    sys.exit(main())
