# Bank CSV Normalizer

Ingests bank-exported CSVs, validates and normalizes transaction data,
deduplicates against a persistent per-account index, emits a unified
output format, and imports that output into Firefly III via its REST API.
Runs unattended from a TrueNAS cron job.

## Tech Stack

- **Python 3.10+** — processing engine ([engine/](engine/))
- **Bash** — cron-safe orchestrator with `flock` and upload check (ctime ≥ 30 s + complete last line); idle runs use builtins only
- **YAML** — per-bank configuration ([config/](config/))
- **Dependencies** — Python stdlib + `pyyaml` only (no build step; the Firefly client uses `urllib`)

## Key Directories

| Path | Purpose |
|------|---------|
| [engine/process_csv.py](engine/process_csv.py) | Normalizer entry point; orchestrates all stages. `main()` + `NORMALIZED_FIELDNAMES` |
| [engine/core/](engine/core/) | Shared pipeline modules: `csv_runtime`, `csv_validation`, `duplicate_index`, `completion`, `runtime` |
| [engine/banks/](engine/banks/) | One sub-package per bank. Each must export `normalize_row()` |
| [engine/banks/fintro/](engine/banks/fintro/) | Reference bank: `normalize_row`, `extract_details`, `parsers`, `reconcile`; `debug_row` shows how a row is parsed |
| [engine/regression.py](engine/regression.py) | Regression test: every row of the bank CSVs through a git ref and the working tree, reporting each changed result |
| [engine/firefly/](engine/firefly/) | Firefly III import: `api` (REST client), `import_normalized` (importer) |
| [deploy/](deploy/) | Source of root-side helper scripts; installed by hand on each server, never run from here (see "Root helper") |
| [config/](config/) | `<bank>.yaml` configs (bank name is the filename) + `app.env` (`FIREFLY_URL`, `FIREFLY_TOKEN`) |
| [bank-csv-normalizer.bash](bank-csv-normalizer.bash) | Cron entry; `flock`, upload check (ctime ≥ 30 s + complete last line), normalizes each incoming CSV, then runs the importer |
| `bank-csv-originals/` | Backup of every unique bank export; source for regenerating `data/` |
| `data/incoming/` | Drop CSVs here to trigger processing |
| `data/normalized/` | Normalized output waiting for import (timestamped) |
| `data/imported/` | Normalized files after import: `<ts>-<name>-imported.csv` (`-imported-partial` if any row of the bank CSV failed, in the normalizer or the import; `-imported-retry[-partial]` for a retried `-import-failed` file) |
| `data/processed/` | Originals after processing (success / partial / failed) |
| `data/failed/` | Rows that failed normalization, dedup, or import; whole files bash moved after a crash |
| `data/duplicate-index/` | Per-account persistent dedup index (successfully normalized rows only) + backups rotated per account |
| `data/logs/` | Per-run logs: `<ts>-<name>.log` (normalizer) and `<ts>-<name>-import.log`; `<ts>` = normalizer run, so all files of one bank CSV sort together |
| `data/temp/` | Working files; cleaned up after each run, except after a critical error (it then holds the index rollback copy) |

The token lives only in the server copy of `config/app.env` (mode 600); the
SFTP watcher excludes that file, so a local copy is never uploaded over it.

## Running

```bash
# Manual single run (normalize data/incoming/, then import data/normalized/)
./bank-csv-normalizer.bash

# TrueNAS cron job: every minute, as the user that owns the folder, "Hide Standard Error" off
# (stderr = alert email), with the encrypted-dataset lock-guard in the Command field

# Normalizer only (debugging)
PYTHONPATH=. python3 -m engine.process_csv <csv_path> <YYYYMMDD-HHMMSS> <logfile_path>

# Debug failing rows: every parser step, the values found, and the exact failure.
# <line> = "source row" in the normalizer log <ts>-<name>.log, or a Volgnummer (reads only)
PYTHONPATH=. python3 -m engine.banks.fintro.debug_row <csv> <line-or-Volgnummer> [...]

# Importer only; --dry-run builds every request but sends and moves nothing
PYTHONPATH=. python3 -m engine.firefly.import_normalized [--dry-run] [--show N] [csv ...]
```

Alerts go to stderr ([engine/core/runtime.py](engine/core/runtime.py) `alert`);
success is silent.

## Lint

```bash
ruff check .
```
Config: [ruff.toml](ruff.toml) — line-length 120, py310 target, selects `E,F,W,I,UP,B`.

Regression test, before every parser change is committed (desktop, in the repo;
the originals are only read via the share; exit 1 when anything changed):

```bash
python -m engine.regression [--base REF] [--show N] "//<server>/bank-csv-normalizer/bank-csv-originals/*.csv"
```

Every reported change must be explained: fixed rows, and changed outputs that
only add information. Verify the normalizer end to end on the server (via the share; `data/` is not
synced) by placing a sample CSV in `data/incoming/`
and inspecting `data/normalized/`, `data/failed/`, and `data/logs/` (failure
reasons are in the normalizer log, not the `-import.log`); verify
the importer with `--dry-run` on the server (the token lives there).

## Exit Codes

Normalizer — [engine/process_csv.py](engine/process_csv.py) and
[engine/core/completion.py](engine/core/completion.py):

| Code | Outcome |
|------|---------|
| `0` | success, all_full_duplicates, or all_filtered (nothing to do) |
| `65` | structure_failed / all_failed |
| `75` | partial (some rows failed) |
| `92–97` | critical file operation errors |
| `99` | unexpected exception |

Importer — [engine/firefly/import_normalized.py](engine/firefly/import_normalized.py):
`0` all imported, `75` some rows failed, `69` Firefly unreachable or token refused
(files stay in `data/normalized/`; alerted once per outage via
`data/firefly-import-blocked.flag`), `70` unexpected crash (files stay; traceback
alerted once via `data/firefly-import-crashed.flag`, removed by the next run that
ends without crashing). `data/firefly-recalculate.flag` marks a batch
follow-up that is still pending (see below).

## Normalization principle

**No meaningful information may be lost.** Every piece of the bank row that says
something about the transaction must end up in a normalized field. The parser
may drop only text that adds nothing — a filler or label (`VAN`, `MEDEDELING :`)
or a repeat of something already kept — and only explicitly, through a named
pattern or rule. Anything it cannot place makes the row fail; the fix then goes
into the parser (`debug_row` shows where a row gets stuck).

## Fintro message markers

Incoming payments from employers, health funds and unions start their free text
with `/A/`, `/B/` or `/C/`. The marker stays in `description` unchanged. By payer
(inferred from the data, no official definition found): `/A/` wages (employers),
`/B/` replacement-income benefits (health fund, union, unemployment fund),
`/C/` reimbursed care (health fund, lines like `PREST HUISARTS`). Meant for
filtering/tagging later.

## Firefly III Import

One `POST /api/v1/transactions` per row, with `error_if_duplicate_hash`, so
re-importing a file is safe. Two modes, chosen per run (`BATCH_OLD_ROW_AGE_DAYS`,
`BATCH_OLD_ROWS_THRESHOLD`; the age cutoff is relative to the day of the run):

- **Per row** (default; ~0.3 s per recent row): Firefly applies rules and
  recalculates running balances immediately. Cost grows with the age of the
  row, because every later balance of the account is recalculated (~3 s for a
  2015 row).
- **Batch** (more than 20 rows older than 6 months, i.e. history imports;
  ~0.2 s per row): rows are submitted with `batch_submission`. At the end of the
  run `batch/finish` applies the rules and the root helper recalculates all
  running balances (~1 min at 14,500 transactions).
  `data/firefly-recalculate.flag` stays until both succeeded, so an interrupted
  or failed follow-up is retried by the next run, even without new files. A
  failure alerts once; `data/firefly-follow-up-alerted.flag` keeps the retries
  every minute silent until the follow-up succeeds.

Rows Firefly rejected go to `data/failed/<ts>-<name>-import-failed.csv`. Dropping the
bank CSV again does not retry them: the duplicate index already has them. Fix the
cause, then move that file to `data/normalized/`; the next run imports it (the
alert names the exact paths).

Row → split mapping lives in `build_split()`:

- Sign of `amount` decides withdrawal / deposit; `asset_account_iban` must match
  a Firefly asset account (looked up by IBAN every run).
- A counterparty account without an IBAN (foreign account number) goes to
  `opposing_account_number` → `source_number` / `destination_number`: Firefly
  validates `*_iban` as an IBAN, `*_number` is free text.
- Counterparty IBAN of an own asset account → **transfer**, deduplicated by
  "match or create": the first side creates it, the other side claims it
  (same accounts, same amount, ±7 days). Order and history coverage of the
  accounts' CSVs don't matter.
- Empty description → counterparty name → first line of `notes` (Firefly
  requires a description).
- No counterparty → `(onbekend)`, except cash withdrawals (Firefly's Cash
  account). Fintro's own transactions get counterparty `Fintro` in the
  normalizer (`BANK_COUNTERPARTY_TRANSACTION_TYPES`).

Gotchas:

- **Firefly ≥ 6.7.0 is required.** Before it (issue #12710) the duplicate hash
  included the batch flag, so a row imported per row and again in batch mode
  was stored twice.
- **`enable_batch_processing` must be on** (Firefly admin configuration, set it
  in the GUI: the configuration API stores it as text, which Firefly ignores).
  While it is off, batch mode silently does the full per-row work.
- Firefly's duplicate check includes **deleted** transactions. After deleting
  transactions, also `DELETE /api/v1/data/purge`, or a re-import silently
  counts them as "already present".
- `DELETE /api/v1/data/destroy` of many objects answers 504 after about a
  minute and stops partway: repeat it until 204, then purge. Destroying
  transactions does not delete the expense/revenue accounts they created
  (`objects=expense_accounts` / `revenue_accounts`).
- Dates must be ISO; Firefly parses `dd/mm/yyyy` as US month/day without error.
- Personal Access Tokens expire after at most one year → "FIREFLY IMPORT BLOCKED" alert.

## Root helper (running balances)

Firefly has no API to recalculate running balances, only
`php artisan firefly-iii:refresh-running-balance --force` inside its container,
and `docker exec` needs root. So the cron user may run exactly one script as
root, through a passwordless sudo rule:

- Source: [deploy/firefly-refresh-running-balance.bash](deploy/firefly-refresh-running-balance.bash).
  It takes no arguments and finds the container by name (exactly one running
  `firefly` container that is not importer/cron/database/redis), so it works on
  every server.
- Install per server, root-owned in a folder the cron user cannot write:

  ```bash
  install -d -o root -g root -m 755 <app-ds>/root-scripts
  install -o root -g root -m 755 <app-ds>/bank-csv-normalizer/deploy/firefly-refresh-running-balance.bash <app-ds>/root-scripts/
  ```

- Sudo rule: TrueNAS GUI → Credentials → Users → the cron user → "Allowed sudo
  commands with no password" (stored in the config database, so it survives
  updates): the full path of the installed script, no wildcards (a wildcard
  would give root in any container).
- The importer runs it as `sudo -n <script>` (`REFRESH_SCRIPT`). Missing script,
  rule or container gives one alert, and the flag keeps the follow-up pending.

## Start Over (full reload)

Rows already in the duplicate index never reach the parser again, so a parser
change only affects new rows. To give already imported transactions the new
output too, wipe Firefly and `data/` and reload every original. Not needed for
a renamed bank column (add the name to `names`) or a changed `columns.required`
(the index adapts, see architectural_patterns.md §7). A column the bank only
recently added is absent from older originals: reloading fills it only after a
fresh history export.

Takes about an hour for ~14,500 rows (batch mode plus the follow-up). Run the
regression test first (desktop); the commands below run on the server, as the
cron user. Wait until `data/incoming/` and `data/normalized/` are empty, so no
run is busy.

Wipe Firefly's transactions and the expense/revenue accounts they created
(asset accounts, rules and categories stay; the importer needs the asset
accounts' IBANs). `destroy` stops partway with a 504, hence the loops:

```bash
cd <app-ds>/bank-csv-normalizer && . config/app.env
ff() { curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer ${FIREFLY_TOKEN}" -H 'Accept: application/json' "${FIREFLY_URL}/api/v1/data/$1"; }
for objects in transactions expense_accounts revenue_accounts; do until code=$(ff "destroy?objects=${objects}"); echo "${objects} ${code}"; [ "${code}" = 204 ]; do :; done; done
echo "purge $(ff purge)"
```

Every line ends in `504` (repeated) or `204`; anything else (`401`: token)
loops forever, so stop it with Ctrl-C. `purge` must print `204`. Then empty `data/` (the originals stay) and
reload them; the cron picks them up within a minute:

```bash
rm -rf data/duplicate-index data/normalized data/imported data/processed data/failed data/logs data/temp data/*.flag
cp bank-csv-originals/*.csv data/incoming/
```

## Adding a New Bank

1. Create `config/<bank>.yaml` — required/optional columns, regex rules,
   filter values, `duplicate_key`. See [config/fintro.yaml](config/fintro.yaml).
   Bank name is derived from the filename by `load_all_bank_configs()`
   in [engine/core/csv_runtime.py](engine/core/csv_runtime.py) — no `bank:` field needed.
2. Create `engine/banks/<bank>.py` (or `engine/banks/<bank>/` package
   exposing `normalize_row` in `__init__.py`). See [engine/banks/fintro/](engine/banks/fintro/).
3. No further code changes — `autodetect_bank()` in
   [engine/core/csv_validation.py](engine/core/csv_validation.py) matches configs by
   header, and `process_csv.py` imports the bank module dynamically via
   `importlib.import_module(f"engine.banks.{bank_name}")`. The importer needs
   no change either, as long as the account exists in Firefly with its IBAN.

## Hooks / Settings

[.claude/settings.local.json](.claude/settings.local.json) only whitelists
`ruff check`, `pre-commit run`, and `git add` for permission prompts. No hooks
are configured at project level.

## Additional Documentation

See [.claude/rules/architectural_patterns.md](.claude/rules/architectural_patterns.md)
for design patterns, pipeline phases, reconciliation strategy, and error
criticality tiers.
