"""
Import normalized CSVs from data/normalized/ into Firefly III via its REST API.

One POST per transaction; runs with many old rows use batch mode. Per file:
  - every row imported / already present -> file moved to data/imported/
    (as -imported-partial when the normalizer already dropped rows of the bank CSV)
  - some rows failed -> failed rows to data/failed-rows/<base>-import-failed.csv,
    file moved to data/imported/ as -imported-partial, alert on stderr; moving that
    failed file into data/normalized/ retries it (-> -imported-retry[-partial])
  - Firefly unreachable or token refused -> run stops, file stays for the next run;
    alerted once per outage (flag file), not every cron minute

Re-running a file is safe: error_if_duplicate_hash makes Firefly reject a
transaction it already stored, which counts as "already present".

Transfers between own asset accounts appear in both accounts' CSVs but are one
transaction in Firefly ("match or create"): a row whose counterparty is an own
asset account first looks for an existing transfer between the same two
accounts, same amount, dated within TRANSFER_MATCH_DAYS. Each transfer can be
claimed once per side (outgoing / incoming). Found -> already present;
not found -> the row creates the transfer. Order and history coverage of the
accounts' CSVs therefore don't matter.

Usage:
  PYTHONPATH=. python3 -m engine.firefly.import_normalized [--dry-run] [--show N] [csv ...]
"""

import argparse
import collections
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from engine.core.runtime import BASE_DIR, CONFIG, alert, log_event
from engine.firefly.api import FireflyAuthError, FireflyClient, FireflyUnavailableError

DATA_DIR = os.path.join(BASE_DIR, "data")
NORMALIZED_DIR = os.path.join(DATA_DIR, "normalized")
IMPORTED_DIR = os.path.join(DATA_DIR, "imported")
FAILED_DIR = os.path.join(DATA_DIR, "failed-rows")
LOG_DIR = os.path.join(DATA_DIR, "logs")
BLOCKED_FLAG = os.path.join(DATA_DIR, "firefly-import-blocked.flag")
# Set while batch-submitted transactions still need rules + running balances
RECALCULATE_FLAG = os.path.join(DATA_DIR, "firefly-recalculate.flag")
# Set after the first failed follow-up alert, so a retry every cron minute stays silent
FOLLOW_UP_ALERTED_FLAG = os.path.join(DATA_DIR, "firefly-follow-up-alerted.flag")
# Set by an unexpected crash, so the retry every cron minute alerts only once
CRASHED_FLAG = os.path.join(DATA_DIR, "firefly-import-crashed.flag")
# Excel's lock file next to a CSV opened through the share: not an output (skipped, as in bash)
OFFICE_LOCK_PREFIX = "~$"

# Per row, Firefly recalculates every later balance of the account (~2 ms per later
# transaction): recent rows cost ~0.3 s, old rows seconds each. Batch costs ~0.2 s
# per row plus a fixed follow-up (~1 min), so it pays off from ~20 old rows.
# The cutoff is relative to the day of the run.
BATCH_OLD_ROW_AGE_DAYS = 183
BATCH_OLD_ROWS_THRESHOLD = 20
RECALCULATE_TIMEOUT_SECONDS = 3600
# batch/finish applies rules to every pending transaction: minutes after a history import
FINISH_TIMEOUT_SECONDS = 1800
# Root-owned, next to this app's folder (source: deploy/); run via a sudo rule
REFRESH_SCRIPT = os.path.join(os.path.dirname(BASE_DIR), "root-scripts", "firefly-refresh-running-balance.bash")

EXIT_OK = 0
EXIT_UNAVAILABLE = 69
EXIT_CRASHED = 70
EXIT_PARTIAL = 75

TRANSFER_MATCH_DAYS = 7
UNKNOWN_COUNTERPARTY = "(onbekend)"

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_DATE_OPTIONAL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
RE_AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")
RE_DUPLICATE = re.compile(r"Duplicate of transaction #(\d+)")

# (source_id, destination_id, amount) -> transfers: {"date": date, "out": claimed, "in": claimed}
TransferPool = dict[tuple[str, str, str], list[dict[str, Any]]]


def clean_identifier(value: str | None) -> str:
    """IBAN or account number without spaces, upper case."""
    return (value or "").replace(" ", "").upper()


def money(value: str) -> str:
    """Canonical amount for matching; Firefly returns amounts with many decimals."""
    return str(Decimal(value).copy_abs().quantize(Decimal("0.01")))


# ---------------------------------------------------------------------------
# Firefly state
# ---------------------------------------------------------------------------
# IBAN or account number -> {"id", "name"}; None when several accounts share it
Assets = dict[str, dict[str, str] | None]


def load_asset_accounts(client: FireflyClient) -> Assets:
    """Every Firefly asset account, findable by its IBAN and by its account number."""
    assets: Assets = {}
    for account in client.get_all("accounts?type=asset"):
        attributes = account["attributes"]
        found = {"id": str(account["id"]), "name": attributes["name"]}
        for identifier in {clean_identifier(attributes.get(field)) for field in ("iban", "account_number")} - {""}:
            assets[identifier] = found if assets.get(identifier, found) is found else None
    return assets


def find_asset(assets: Assets, *identifiers: str | None) -> dict[str, str] | None:
    """The own account of the first identifier Firefly knows; ValueError if several accounts share it."""
    for identifier in map(clean_identifier, identifiers):
        if identifier in assets:
            if assets[identifier] is None:
                raise ValueError(f"{identifier} is the IBAN or account number of several Firefly asset accounts")
            return assets[identifier]
    return None


def load_currencies(client: FireflyClient) -> dict[str, bool]:
    """Code -> enabled, for every currency Firefly knows."""
    return {c["attributes"]["code"]: bool(c["attributes"]["enabled"]) for c in client.get_all("currencies")}


def ensure_currencies(
    split: dict[str, Any], currencies: dict[str, bool], client: FireflyClient, dry_run: bool, log: Callable[[str], None]
) -> None:
    """
    Enable a disabled currency the split uses (Firefly stores the transaction either way).
    A currency Firefly doesn't know raises ValueError: creating one needs a name and symbol.
    """
    for code in (split.get("currency_code"), split.get("foreign_currency_code")):
        if not code or currencies.get(code):
            continue
        if code not in currencies:
            raise ValueError(f"Currency {code} unknown to Firefly")
        if dry_run:
            log(f"Would enable currency {code}")
        else:
            status, response = client.request("POST", f"currencies/{code}/enable")
            if status not in (200, 204):
                raise ValueError(f"Enabling currency {code}: HTTP {status}: {response.get('message', '')}")
            log(f"Enabled currency {code}")
        currencies[code] = True


def load_transfer_pool(client: FireflyClient) -> TransferPool:
    pool: TransferPool = collections.defaultdict(list)
    for group in client.get_all("transactions?type=transfer"):
        for split in group["attributes"]["transactions"]:
            key = (str(split["source_id"]), str(split["destination_id"]), money(split["amount"]))
            pool[key].append({"date": date.fromisoformat(split["date"][:10]), "out": False, "in": False})
    return pool


def claim_transfer(pool: TransferPool, key: tuple[str, str, str], on: date, side: str) -> bool:
    """Claim the closest unclaimed transfer for this side within the match window."""
    candidates = [t for t in pool.get(key, []) if not t[side] and abs((t["date"] - on).days) <= TRANSFER_MATCH_DAYS]
    if not candidates:
        return False
    min(candidates, key=lambda t: abs((t["date"] - on).days))[side] = True
    return True


# ---------------------------------------------------------------------------
# Row -> Firefly split
# ---------------------------------------------------------------------------
def build_split(
    row: dict[str, str],
    assets: Assets,
) -> tuple[str, dict[str, Any], tuple[tuple[str, str, str], str] | None]:
    """
    Return (decision, split, transfer_match). decision: withdrawal / deposit / transfer.
    transfer_match is ((source_id, destination_id, amount), side) for own transfers. Raises ValueError.
    """
    amount = row["amount"].strip()
    if not RE_AMOUNT.match(amount) or Decimal(amount) == 0:
        raise ValueError(f"Invalid or zero amount: '{amount}'")

    for field, pattern in (
        ("primary_transaction_date", RE_DATE),
        ("interest_date", RE_DATE),
        ("transaction_processing_date", RE_DATE),
        ("payment_date", RE_DATE_OPTIONAL_TIME),
    ):
        value = row.get(field, "")
        if value and not pattern.match(value):
            raise ValueError(f"{field} not ISO: '{value}'")
        try:
            if value:
                date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"{field} is no date: '{value}' ({exc})") from None
    if not row.get("primary_transaction_date"):
        raise ValueError("primary_transaction_date is empty")

    foreign_amount = row.get("foreign_amount", "")
    foreign_currency = row.get("foreign_currency_code", "")
    if foreign_amount and not RE_AMOUNT.match(foreign_amount):
        raise ValueError(f"Invalid foreign_amount: '{foreign_amount}'")
    if bool(foreign_amount) != bool(foreign_currency):
        raise ValueError(
            f"foreign_amount '{foreign_amount}' and foreign_currency_code '{foreign_currency}': both or neither"
        )

    # Columns absent in files normalized before they existed
    own_iban = clean_identifier(row.get("asset_account_iban"))
    own_number = row.get("asset_account_number") or ""
    own = find_asset(assets, own_iban, own_number)
    if not own:
        raise ValueError(f"No Firefly asset account with IBAN or account number {own_iban or own_number}")

    opposing_iban = clean_identifier(row.get("opposing_account_iban"))
    opposing_number = row.get("opposing_account_number") or ""
    opposing_asset = find_asset(assets, opposing_iban, opposing_number)
    if opposing_asset is own:
        raise ValueError(f"Counterparty {opposing_iban or opposing_number} is the row's own account")
    outgoing = amount.startswith("-")

    # Without a counterparty Firefly books on its Cash account: right for cash withdrawals only
    opposing_name = row.get("opposing_account_name", "")
    if not opposing_name and not opposing_iban and not opposing_number:
        opposing_name = "" if row.get("is_cash_withdrawal") == "1" else UNKNOWN_COUNTERPARTY

    # Firefly requires a description; fall back to what identifies the transaction best
    notes = row.get("notes", "")
    description = row.get("description", "") or opposing_name or notes.split("\n", 1)[0]

    split: dict[str, Any] = {
        "date": row["primary_transaction_date"],
        "amount": amount.lstrip("-"),
        "currency_code": row.get("account_currency_code", ""),
        "foreign_amount": foreign_amount.lstrip("-"),
        "foreign_currency_code": foreign_currency,
        "description": description,
        "notes": notes,
        "external_id": row.get("external_id", ""),
        "internal_reference": row.get("row_key", ""),
        "interest_date": row.get("interest_date", ""),
        "process_date": row.get("transaction_processing_date", ""),
        "payment_date": row.get("payment_date", ""),
    }

    transfer_match = None
    if opposing_asset:
        source, destination = (own, opposing_asset) if outgoing else (opposing_asset, own)
        split.update(type="transfer", source_id=source["id"], destination_id=destination["id"])
        transfer_match = ((source["id"], destination["id"], money(amount)), "out" if outgoing else "in")
        decision = "transfer"
    elif outgoing:
        split.update(
            type="withdrawal",
            source_id=own["id"],
            destination_name=opposing_name,
            destination_iban=opposing_iban,
            destination_number=opposing_number,
            destination_bic=row.get("opposing_account_bic", ""),
        )
        decision = "withdrawal"
    else:
        split.update(
            type="deposit",
            destination_id=own["id"],
            source_name=opposing_name,
            source_iban=opposing_iban,
            source_number=opposing_number,
            source_bic=row.get("opposing_account_bic", ""),
        )
        decision = "deposit"

    # Firefly treats an absent key as "not set"; empty strings can trip its validators
    return decision, {key: value for key, value in split.items() if value != ""}, transfer_match


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------
def firefly_answers(client: FireflyClient) -> bool:
    """True when Firefly answers a plain request: it is up."""
    try:
        status, _ = client.request("GET", "about")
    except (FireflyAuthError, FireflyUnavailableError):
        return False
    return status == 200


def import_file(
    path: str,
    client: FireflyClient,
    assets: Assets,
    currencies: dict[str, bool],
    pool: TransferPool,
    batch: bool,
    dry_run: bool,
    show: int,
    run_note: str,
) -> collections.Counter:
    name = os.path.basename(path)
    # '<base>-normalized[-partial].csv' or a retried '<base>-import-failed.csv' -> '<base>':
    # every output keeps the normalizer's name, so all files of one bank file sort together
    stem = os.path.splitext(name)[0]
    base = re.sub(r"-(normalized(-partial)?|import-failed)$", "", stem)
    normalized_partial = stem.endswith("-normalized-partial")
    retry = stem.endswith("-import-failed")
    logfile = os.path.join(LOG_DIR, f"{base}-import.log")

    def log(message: str) -> None:
        if dry_run:
            print(f"  {message}")
        else:
            log_event(logfile, message)

    counts: collections.Counter = collections.Counter()
    failed: list[tuple[int, dict[str, str], str]] = []
    shown = 0
    started = time.monotonic()

    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    log(f"Importing {name}: {len(rows)} rows, oldest first; {run_note}")

    # Oldest first: per row, Firefly recalculates the balance of every later transaction of
    # the account, and banks export newest first. Within a day, reverse file order (= oldest
    # first for a newest-first export). Line numbers stay those of the file.
    numbered = sorted(
        enumerate(rows, start=2), key=lambda item: (item[1].get("primary_transaction_date", ""), -item[0])
    )
    for line_no, row in numbered:
        try:
            decision, split, transfer_match = build_split(row, assets)
            ensure_currencies(split, currencies, client, dry_run, log)
        except (ValueError, KeyError, ArithmeticError) as exc:
            failed.append((line_no, row, f"line {line_no}: {exc}"))
            counts["failed"] += 1
            continue

        if transfer_match:
            key, side = transfer_match
            on = date.fromisoformat(split["date"])
            if claim_transfer(pool, key, on, side):
                counts["transfer already present"] += 1
                continue

        if dry_run:
            status, response = 200, {}
            if shown < show:
                print(json.dumps(split, ensure_ascii=False))
                shown += 1
        else:
            body = {
                "error_if_duplicate_hash": True,
                "batch_submission": batch,
                "apply_rules": True,
                "fire_webhooks": True,
                "transactions": [split],
            }
            try:
                status, response = client.request("POST", "transactions", body)
            except FireflyUnavailableError as exc:
                # A 5xx while Firefly answers other requests is this row's fault: failing only the row
                # keeps one row Firefly always crashes on from blocking every run
                if exc.status is None or not firefly_answers(client):
                    raise FireflyUnavailableError(f"{name} line {line_no}: {exc}") from exc
                status, response = exc.status, {"message": "Firefly crashed on this row; it answers other requests"}

        if status == 200:
            counts[decision] += 1
        elif status == 422 and RE_DUPLICATE.search(json.dumps(response)):
            counts["already present"] += 1
        else:
            reason = f"line {line_no}: HTTP {status}: {response.get('message', '')} {response.get('errors', '')}"
            failed.append((line_no, row, reason))
            counts["failed"] += 1
            log(reason)
            continue

        if transfer_match:
            # The other side of this transfer must find it later, in this run or a future one
            key, side = transfer_match
            pool.setdefault(key, []).append(
                {"date": date.fromisoformat(split["date"]), "out": side == "out", "in": side == "in"}
            )

    log(f"Result {name}: {dict(counts)} in {time.monotonic() - started:.0f} s")
    failed.sort(key=lambda item: item[0])  # back in file order
    for _, _, reason in failed:
        log(f"FAILED {reason}")

    if dry_run:
        return counts

    # -partial: not every row of the bank CSV reached Firefly (normalizer or import)
    kind = "imported-retry" if retry else "imported"
    target_name = f"{base}-{kind}{'-partial' if normalized_partial else ''}.csv"
    if failed:
        failed_path = os.path.join(FAILED_DIR, f"{base}-import-failed.csv")
        # A retried file already carries import_error: replace it, don't add a second one
        fieldnames = [field for field in rows[0] if field != "import_error"] + ["import_error"]
        with open(failed_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for _, row, reason in failed:
                writer.writerow({**row, "import_error": reason})
        target_name = f"{base}-{kind}-partial.csv"
        reasons = "\n".join(reason for _, _, reason in failed[:20])
        alert(
            f"FIREFLY IMPORT PARTIAL: {name} ({counts['failed']} of {len(rows)} rows failed)",
            f"File: {name}\nFailed rows: {failed_path}\nLog: {logfile}\n{dict(counts)}\n\n{reasons}\n\n"
            f"Retry after fixing the cause: move {failed_path} to {NORMALIZED_DIR}/",
        )
    shutil.move(path, os.path.join(IMPORTED_DIR, target_name))
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def count_old_rows(path: str, cutoff: str) -> int:
    """Rows dated before cutoff (ISO, so string comparison is date comparison)."""
    with open(path, encoding="utf-8", newline="") as f:
        return sum(
            1
            for row in csv.DictReader(f, delimiter=";")
            if RE_DATE.match(row.get("primary_transaction_date", "")) and row["primary_transaction_date"] < cutoff
        )


def alert_follow_up(subject: str, body: str) -> None:
    """Alert once while the follow-up keeps failing; the cron retries every minute."""
    if not os.path.exists(FOLLOW_UP_ALERTED_FLAG):
        alert(subject, f"{body}\n\nNo further alerts until it succeeds.")
    with open(FOLLOW_UP_ALERTED_FLAG, "w", encoding="utf-8") as f:
        f.write(subject + "\n")


def finish_batch(client: FireflyClient) -> bool:
    """
    Apply rules to every batch-submitted transaction (including those of an
    interrupted run). It does not recalculate running balances: Firefly's batch
    event carries no accounts, hence recalculate_running_balances().
    """
    # Firefly compares apply_rules to the string "true"
    try:
        status, response = client.request("POST", "batch/finish?apply_rules=true", timeout=FINISH_TIMEOUT_SECONDS)
    except FireflyUnavailableError as exc:
        # Slow is not down: keep the follow-up pending instead of blocking imports
        alert_follow_up("FIREFLY BATCH FINISH FAILED", f"{exc}\n\nRules are pending; the next import run retries.")
        return False
    if status in (200, 204):
        return True
    alert_follow_up(
        f"FIREFLY BATCH FINISH FAILED: HTTP {status}",
        f"{response}\n\nRules are pending; the next import run retries.",
    )
    return False


def recalculate_running_balances() -> bool:
    """
    Run Firefly's own full recalculation through REFRESH_SCRIPT, which finds the
    Firefly container itself. Needs, per server: the script installed root-owned,
    and a TrueNAS sudo rule (no password) for it for the user running the cron job.
    """
    command = ["sudo", "-n", REFRESH_SCRIPT]
    subject = "FIREFLY RUNNING BALANCES NOT RECALCULATED"
    footer = f"\n\nRetried on the next import run while {RECALCULATE_FLAG} exists."
    if not os.path.isfile(REFRESH_SCRIPT):
        alert_follow_up(subject, f"{REFRESH_SCRIPT} is not installed (source: deploy/).{footer}")
        return False
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=RECALCULATE_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        alert_follow_up(subject, f"{' '.join(command)}\n{exc}{footer}")
        return False
    if result.returncode != 0:
        alert_follow_up(subject, f"{' '.join(command)}\nexit {result.returncode}\n{result.stderr[-2000:]}{footer}")
        return False
    return True


def set_blocked(reason: str) -> None:
    """Alert once per outage; the cron runs every minute."""
    if not os.path.exists(BLOCKED_FLAG):
        alert(f"FIREFLY IMPORT BLOCKED: {reason}", f"{reason}\n\nFiles stay in {NORMALIZED_DIR} and are retried.")
    with open(BLOCKED_FLAG, "w", encoding="utf-8") as f:
        f.write(reason + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="build every request, send nothing, move nothing")
    parser.add_argument("--show", type=int, default=0, help="dry-run: print the first N request payloads per file")
    parser.add_argument("files", nargs="*", help=f"default: every *.csv in {NORMALIZED_DIR}")
    args = parser.parse_args()

    # A dry run is interactive: let a crash show its traceback
    if args.dry_run:
        return import_all(args)
    try:
        exit_code = import_all(args)
    except Exception:
        # Files stay in data/normalized/, so the cron retries every minute: alert once
        details = traceback.format_exc()
        if not os.path.exists(CRASHED_FLAG):
            alert(
                "FIREFLY IMPORT CRASHED",
                f"{details}\nFiles stay in {NORMALIZED_DIR}; every cron minute retries. "
                "No further alerts until a run ends without crashing.",
            )
        with open(CRASHED_FLAG, "w", encoding="utf-8") as f:
            f.write(details)
        return EXIT_CRASHED
    if os.path.exists(CRASHED_FLAG):
        os.remove(CRASHED_FLAG)
    return exit_code


def import_all(args: argparse.Namespace) -> int:
    files = args.files or sorted(
        path
        for path in glob.glob(os.path.join(NORMALIZED_DIR, "*.csv"))
        if not os.path.basename(path).startswith(OFFICE_LOCK_PREFIX)
    )
    # A pending batch follow-up is retried even when there is nothing new to import
    follow_up_pending = not args.dry_run and os.path.exists(RECALCULATE_FLAG)
    if not files and not follow_up_pending:
        return EXIT_OK

    if not args.dry_run:
        for d in (IMPORTED_DIR, FAILED_DIR, LOG_DIR):
            os.makedirs(d, exist_ok=True)

    totals: collections.Counter = collections.Counter()
    try:
        client = FireflyClient(CONFIG.get("FIREFLY_URL", ""), CONFIG.get("FIREFLY_TOKEN", ""))
        if files:
            started = time.monotonic()
            assets = load_asset_accounts(client)
            currencies = load_currencies(client)
            pool = load_transfer_pool(client)
            cutoff = (date.today() - timedelta(days=BATCH_OLD_ROW_AGE_DAYS)).isoformat()
            old_rows = sum(count_old_rows(path, cutoff) for path in files)
            batch = old_rows > BATCH_OLD_ROWS_THRESHOLD
            # Logged per file, so each import log says how the run went about it
            run_note = (
                f"mode {'batch' if batch else 'per row'} ({old_rows} rows before {cutoff} in this run); "
                f"read {len({id(a) for a in assets.values() if a})} asset accounts, {len(currencies)} currencies "
                f"and {sum(len(t) for t in pool.values())} transfers "
                f"in {time.monotonic() - started:.0f} s"
            )
            if args.dry_run:
                print(f"== {run_note}")
            elif batch:
                # Before the first batch row, so an interruption still triggers the follow-up
                open(RECALCULATE_FLAG, "w", encoding="utf-8").close()
            for path in files:
                if args.dry_run:
                    print(f"== {os.path.basename(path)}")
                counts = import_file(path, client, assets, currencies, pool, batch, args.dry_run, args.show, run_note)
                if args.dry_run:
                    print(f"  {dict(counts)}")
                totals.update(counts)
        if not args.dry_run and os.path.exists(RECALCULATE_FLAG):
            # Named after the batch that set the flag: retries every minute append, not one log each
            pending_since = datetime.fromtimestamp(os.path.getmtime(RECALCULATE_FLAG))
            follow_up_log = os.path.join(LOG_DIR, f"{pending_since:%Y%m%d-%H%M%S}-firefly-follow-up.log")
            started = time.monotonic()
            finished = finish_batch(client)
            recalculated = False
            log_event(
                follow_up_log,
                f"batch/finish (rules): {'ok' if finished else 'failed'} in {time.monotonic() - started:.0f} s",
            )
            if finished:
                started = time.monotonic()
                recalculated = recalculate_running_balances()
                log_event(
                    follow_up_log,
                    f"running balances: {'ok' if recalculated else 'failed'} in {time.monotonic() - started:.0f} s",
                )
            if finished and recalculated:
                os.remove(RECALCULATE_FLAG)
                if os.path.exists(FOLLOW_UP_ALERTED_FLAG):
                    os.remove(FOLLOW_UP_ALERTED_FLAG)
    except (FireflyAuthError, FireflyUnavailableError) as exc:
        if args.dry_run:
            print(f"BLOCKED: {exc}", file=sys.stderr)
        else:
            set_blocked(str(exc))
        return EXIT_UNAVAILABLE

    if not args.dry_run and os.path.exists(BLOCKED_FLAG):
        os.remove(BLOCKED_FLAG)
    if args.dry_run:
        print(f"== TOTAL {dict(totals)}")
    return EXIT_PARTIAL if totals["failed"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
