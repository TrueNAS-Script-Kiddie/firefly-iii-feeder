# Firefly III Feeder

Ingests bank-exported CSVs, validates and normalizes transaction data,
deduplicates against a persistent per-account index, emits a unified
output format, and imports that output into Firefly III via its REST API.
Runs unattended from a TrueNAS cron job.

## Tech Stack

- **Python 3.10+** — processing engine ([engine/](engine/))
- **Bash** — cron-safe orchestrator with `flock` and upload check (ctime ≥ 30 s + complete last line); idle runs use builtins only
- **YAML** — per-bank configuration ([config/](config/))
- **Dependencies** — Python stdlib + `pyyaml` only (no build step; the Firefly client uses `urllib`)

## Idle cost (hard rule)

The cron job runs every minute, so an idle run must cost next to nothing. Until there is
work (a CSV in `data/incoming/` or `data/normalized/`, or a pending
`firefly-recalculate.flag`), `firefly-iii-feeder.bash` uses bash builtins only: no subshell,
no external command, no Python, no file write. Every new check goes after that exit.

Measured on truenas-master: 1.9 ms CPU per idle run (bash alone: 1.2 ms); starting Python
with the importer's imports costs 60 ms and 17 MB, 30 times as much. Re-measure after
changing the script
(in `/tmp` there is no `data/`, so it takes the idle path and writes nothing; divide by 30):

```bash
cd /tmp && time (for i in {1..30}; do bash -s < <app-ds>/firefly-iii-feeder/firefly-iii-feeder.bash; done)
```

## Key Directories

| Path | Purpose |
|------|---------|
| [engine/process_csv.py](engine/process_csv.py) | Normalizer entry point; orchestrates all stages. `main()` + `NORMALIZED_FIELDNAMES` |
| [engine/core/](engine/core/) | Shared pipeline modules: `csv_runtime`, `csv_validation`, `duplicate_index`, `completion`, `runtime` |
| [engine/banks/](engine/banks/) | One sub-package per bank. Each must export `normalize_row()` and has a `README.md` (see "Banks") |
| [engine/banks/fintro/](engine/banks/fintro/) | Reference bank: `normalize` (exports `normalize_row`), `extract_details`, `parsers`, `reconcile`; `debug_row` shows how a row is parsed |
| [docs/output-contract.md](docs/output-contract.md) | What other tools may rely on in the normalized output and in Firefly |
| `plans/` | Plans for work in progress; once done, their lasting facts move into the docs and the plan is deleted (git keeps it) |
| [engine/regression.py](engine/regression.py) | Regression test: every row of the bank CSVs through a git ref and the working tree, reporting each changed result |
| [engine/firefly/](engine/firefly/) | Firefly III import: `api` (REST client), `import_normalized` (importer) |
| [deploy/](deploy/) | Source of root-side helper scripts; installed by hand on each server, never run from here (see "Root helper") |
| [config/](config/) | `<bank>.yaml` configs (bank name is the filename) + `app.env` (`FIREFLY_URL`, `FIREFLY_TOKEN`) |
| [firefly-iii-feeder.bash](firefly-iii-feeder.bash) | Cron entry; `flock`, upload check (ctime ≥ 30 s + complete last line), normalizes each incoming CSV, then runs the importer |
| `bank-csv-originals/` | Backup of every unique bank export, one subfolder per bank (`Fintro/`); source for regenerating `data/` |
| `data/incoming/` | Drop CSVs here to trigger processing |
| `data/normalized/` | Normalized output waiting for import: `<base>-normalized[-partial].csv` |
| `data/imported/` | Normalized files after import: `<base>-imported.csv` (`-imported-partial` if any row of the bank file failed, in the normalizer or the import; `-imported-retry[-partial]` for a retried `-import-failed` file) |
| `data/processed/` | Originals after processing, own extension: `<base>-processed[-partial\|-failed].<ext>` |
| `data/failed/` | Rows that failed normalization, dedup, or import (`<base>-normalize-failed.csv`, `-duplicate-failed.csv`, `-import-failed.csv`); whole originals bash moved: `<run>-crashed-<source name>` (Python crashed) or `<run>-move-failed-<source name>` (a critical file move failed) |
| `data/duplicate-index/` | Per-account persistent dedup index (successfully normalized rows only) + backups `backups/<run>-<account>-duplicate-index.csv`, rotated per account |
| `data/logs/` | `<base>.log` (normalizer) and `<base>-import.log` (mode, Firefly state read time, result and duration). `<time>-firefly-follow-up.log`: rules and running balances after a batch import, with durations |
| `data/temp/` | Working files; cleaned up after each run, except after a critical error (it then holds the index rollback copy) |

File names in `data/` describe the content: `<base>` = `<run>-<bank>-<account>-<first>_<last>`,
e.g. `20261009-101500-001-fintro-BE68539007547034-2026-03-13_2026-10-07`, so all files of one
bank file sort together. `<run>` = start of the cron run + the file's number in that run (unique
by construction); `<first>_<last>` = first and last transaction date (`date_format` of
`primary_transaction_date` in the bank config). Before the bank is known, `<base>` is
`<run>-unknown-<source name>`, and the log is `<run>.log` until the normalizer renames it. The
source name is on the log's first line.

The token lives only in the server copy of `config/app.env` (mode 600); the
SFTP watcher excludes that file, so a local copy is never uploaded over it.

## Deploy = save

The VS Code SFTP watcher (`.vscode/sftp.json`, local) uploads every saved file at
once, so an edit runs on the server from the next cron minute — before any commit,
and also when Claude saves it. A change to the normalized columns, to file names in
`data/`, or to anything the importer reads must therefore be saved only while
`data/incoming/` and `data/normalized/` on the server are empty and no
`data/*.flag` is pending: check that first (read-only `ls`).

## Running

```bash
# Manual single run (normalize data/incoming/, then import data/normalized/)
./firefly-iii-feeder.bash

# TrueNAS cron job: every minute, as the user that owns the folder, "Hide Standard Error" off
# (stderr = alert email), with the encrypted-dataset lock-guard in the Command field

# Normalizer only (debugging)
PYTHONPATH=. python3 -m engine.process_csv <csv_path> <run> <logfile_path>   # <run>: YYYYMMDD-HHMMSS-NNN

# Debug a row: whether the account's duplicate index already has it (then it is skipped,
# or a conflict shows the differing fields), every parser step, the values found, the exact
# failure. Run on the server: the index is in data/, which is not synced.
# <line> = "source row" in the normalizer log <base>.log, or a Volgnummer (reads only)
PYTHONPATH=. python3 -m engine.banks.fintro.debug_row <csv> <line-or-Volgnummer> [...]

# Importer only; --dry-run builds every request but sends and moves nothing
PYTHONPATH=. python3 -m engine.firefly.import_normalized [--dry-run] [--show N] [csv ...]
```

Alerts go to stderr ([engine/core/runtime.py](engine/core/runtime.py) `alert`);
success is silent.

## Lint

```bash
ruff check .
pre-commit run --all-files   # what every commit runs: ruff, ruff format, shfmt, shellcheck
```
Config: [ruff.toml](ruff.toml) — line-length 120, py310 target, selects `E,F,W,I,UP,B`;
hooks in [.pre-commit-config.yaml](.pre-commit-config.yaml).

Regression test, before every parser change is committed (desktop, in the repo;
the originals are only read via the share; exit 1 when anything changed):

```bash
python -m engine.regression [--base REF] [--show N] "//<server>/firefly-iii-feeder/bank-csv-originals/Fintro/*.csv"
```

Every reported change must be explained: fixed rows, and changed outputs that
only add information. Verify the normalizer end to end on the server (via the share; `data/` is not
synced) by placing a sample CSV in `data/incoming/`
and inspecting `data/normalized/`, `data/failed/`, and `data/logs/` (failure
reasons are in the normalizer log, not the `-import.log`); verify
the importer with `--dry-run` on the server (the token lives there).

Testing a change to `firefly-iii-feeder.bash` on the desktop (Git Bash): run it in
a throwaway copy, never in the repo's own `data/`. Git Bash has no `flock`, so a stub
stands in for it; the upload wait drops to 0 s; without `app.env` the importer ends
with "FIREFLY IMPORT BLOCKED", as expected. Use the `/c/…` form for the folder: a `C:`
in `PATH` splits it.

```bash
T=/c/Users/<you>/AppData/Local/Temp/feeder-test
mkdir -p "$T/bin" "$T/repo/data/incoming"
cp -r engine config firefly-iii-feeder.bash "$T/repo/"
rm -f "$T/repo/config/app.env"
sed -i 's/^UPLOAD_SETTLE_SECONDS=30/UPLOAD_SETTLE_SECONDS=0/' "$T/repo/firefly-iii-feeder.bash"
printf '#!/bin/sh\nexit 0\n' >"$T/bin/flock" && chmod +x "$T/bin/flock"
cp <sample export> "$T/repo/data/incoming/"
cd "$T/repo" && PATH="$T/bin:$PATH" bash ./firefly-iii-feeder.bash
```

## Exit Codes

Normalizer — [engine/process_csv.py](engine/process_csv.py) and
[engine/core/completion.py](engine/core/completion.py):

| Code | Outcome |
|------|---------|
| `0` | success, all_full_duplicates, or all_filtered (nothing to do) |
| `65` | structure_failed / all_failed |
| `75` | partial (some rows failed) |
| `92` | normalized output not moved (index rolled back) |
| `93` | duplicate index not committed |
| `94` | original CSV not moved |
| `97` | duplicate index update not prepared |
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

## Banks

Everything specific to one bank — how to export, its columns, what makes a row
unique, parsing rules, what is dropped on purpose, markers, quirks — is in that
bank's README. Read it before changing the bank's config or module. Claude Code
loads it by itself when working in the bank's folder (`CLAUDE.md` there imports it).

| Bank | Export | Docs |
|------|--------|------|
| Fintro | CSV, one per account | [engine/banks/fintro/README.md](engine/banks/fintro/README.md) |

## Firefly III Import

Categories and tags are out of scope. Firefly rules apply what they can on
import; everything else is classified by a separate tool in a private repo,
through Firefly's API. Nothing personal (counterparty rules, categories, tags)
belongs in this repo.

One `POST /api/v1/transactions` per row, with `error_if_duplicate_hash`, so
re-importing a file is safe. Two modes, chosen per run (`BATCH_OLD_ROW_AGE_DAYS`,
`BATCH_OLD_ROWS_THRESHOLD`; the age cutoff is relative to the day of the run):

- **Per row** (default; ~0.3 s per recent row): Firefly applies rules and
  recalculates running balances immediately. Cost grows with the age of the
  row, because every later balance of the account is recalculated (~3 s for a
  2015 row). Hence each file is sent oldest first (banks export newest first):
  every row is then the latest of its account (newest first roughly doubles
  the time of a few hundred rows).
- **Batch** (more than 20 rows older than 6 months, i.e. history imports;
  ~0.2 s per row): rows are submitted with `batch_submission`. At the end of the
  run `batch/finish` applies the rules and the root helper recalculates all
  running balances (~1 min at 14,500 transactions).
  `data/firefly-recalculate.flag` stays until both succeeded, so an interrupted
  or failed follow-up is retried by the next run, even without new files. A
  failure alerts once; `data/firefly-follow-up-alerted.flag` keeps the retries
  every minute silent until the follow-up succeeds.

Rows Firefly rejected go to `data/failed/<base>-import-failed.csv`. Dropping the
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
  accounts' CSVs don't matter. The transfer carries only the first side's
  data: the claiming side's Volgnummer (`external_id`), notes and bank
  reference stay in its `data/imported/` file, not in Firefly.
- Empty description → counterparty name → first line of `notes` (Firefly
  requires a description).
- No counterparty → `(onbekend)`, except cash withdrawals (Firefly's Cash
  account). A bank's own transactions (fees, interest) get the bank as
  counterparty in its module (see the bank's README).

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
  install -o root -g root -m 755 <app-ds>/firefly-iii-feeder/deploy/firefly-refresh-running-balance.bash <app-ds>/root-scripts/
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
cd <app-ds>/firefly-iii-feeder && . config/app.env
ff() { curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer ${FIREFLY_TOKEN}" -H 'Accept: application/json' "${FIREFLY_URL}/api/v1/data/$1"; }
for objects in transactions expense_accounts revenue_accounts; do until code=$(ff "destroy?objects=${objects}"); echo "${objects} ${code}"; [ "${code}" = 204 ]; do :; done; done
echo "purge $(ff purge)"
```

Every line ends in `504` (repeated) or `204`; anything else (`401`: token)
loops forever, so stop it with Ctrl-C. `purge` must print `204`. Then empty `data/` (the originals stay) and
reload them; the cron picks them up within a minute:

```bash
rm -rf data/duplicate-index data/normalized data/imported data/processed data/failed data/logs data/temp data/*.flag
cp bank-csv-originals/Fintro/*.csv data/incoming/
```

## Adding a New Bank

1. Create `config/<bank>.yaml` — required/optional columns, regex rules,
   filter values, `duplicate_key`, and `date_format` on `primary_transaction_date`
   (else the file names carry no period). See [config/fintro.yaml](config/fintro.yaml).
   Bank name is derived from the filename by `load_all_bank_configs()`
   in [engine/core/csv_runtime.py](engine/core/csv_runtime.py) — no `bank:` field needed.
2. Create the package `engine/banks/<bank>/`, exposing `normalize_row` in
   `__init__.py`. See [engine/banks/fintro/](engine/banks/fintro/).
3. Document it in `engine/banks/<bank>/README.md` with the headings of the Fintro
   README (Export, Columns, Unique row, Field mapping, parsing, Dropped on purpose,
   Markers and conventions, Quirks, Checking), add `engine/banks/<bank>/CLAUDE.md`
   containing `@README.md`, a row in "Banks" above and in README.md, and its
   transaction types and unique key in [docs/output-contract.md](docs/output-contract.md).
4. No further code changes — `autodetect_bank()` in
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
