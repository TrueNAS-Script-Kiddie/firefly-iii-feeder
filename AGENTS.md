# Firefly III Feeder

Ingests bank exports (CSV, xlsx, PDF statements), validates and normalizes transaction data,
deduplicates against a persistent per-account row state, emits a unified
output format, and imports that output into Firefly III via its REST API.
Runs unattended from a TrueNAS cron job. How it works:
[.claude/rules/architectural_patterns.md](.claude/rules/architectural_patterns.md).

## Tech Stack

- **Python 3.10+** — processing engine ([engine/](engine/))
- **Bash** — cron-safe orchestrator ([firefly-iii-feeder.bash](firefly-iii-feeder.bash))
- **YAML** — per-bank configuration ([config/](config/))
- **Dependencies** — Python stdlib + `pyyaml` only (no build step; the Firefly client uses `urllib`)

## English only (hard rule)

Everything in this public repo is English: code, comments, docs, plans, commit messages, file
names. Only text that is the bank's own stays in its language: export column names, message
patterns, and the transaction types the bank prints.

## Nothing personal (hard rule)

The repo is public. Examples are made up, also amounts, account numbers and dates; before every
commit, scan the staged diff for IBAN-like strings and real names. Counterparty rules, categories
and tags do not belong here either: Firefly rules apply what they can on import, and a separate
tool in a private repo classifies the rest through Firefly's API.

## Idle cost (hard rule)

The cron job runs every minute, so an idle run must cost next to nothing. Until there is
work (an export — csv, xlsx, pdf — in `data/incoming/`, a CSV in `data/normalized/`, a pending
`firefly-recalculate.flag` or `archive-push-pending.flag`), `firefly-iii-feeder.bash` uses bash
builtins only: no subshell, no external command, no Python, no file write. Every new check goes
after that exit. Excel's lock files (`~$<name>.csv`, `~$<name>.xlsx`, left next to a file opened
through the share) are no work: bash and the importer skip them.

Measured on truenas-master: 1.9 ms CPU per idle run (bash alone: 1.8 ms); starting Python
with the importer's imports costs 60 ms and 17 MB, 30 times as much. Re-measure after
changing the script
(in `/tmp` there is no `data/`, so it takes the idle path and writes nothing; divide by 30):

```bash
cd /tmp && time (for i in {1..30}; do bash -s < <app-ds>/firefly-iii-feeder/firefly-iii-feeder.bash; done)
```

## Rebuild from scratch (hard rule)

What the feeder puts into Firefly (own accounts and transactions) is derived from the input.
Wiping it and loading it again must always be possible, with one command, and gives the result
of the current code. After every structural change to the feeder or to Firefly, that is the
normal way to give old transactions the new shape, not an emergency.

- All input belongs in the private archive repo `archive/`; `data/` and Firefly hold nothing
  that cannot be remade. How it works, `start-over.bash`, setup and restore:
  [docs/archive-and-reload.md](docs/archive-and-reload.md).
- Fix nothing by hand in `data/` or in Firefly: a fix goes into the parser, the config or the
  accounts file. The only hand fix to input is one in the archive itself, where git keeps it.
- Classification is outside this rule: a wipe keeps rules, categories and tags in Firefly but
  removes them from the transactions; reapplying them is up to the private tool that owns it.

## Normalization principle

**No meaningful information may be lost.** Every piece of the bank row that says
something about the transaction must end up in a normalized field. The parser
may drop only text that adds nothing — a filler or label (`VAN`, `MEDEDELING :`)
or a repeat of something already kept — and only explicitly, through a named
pattern or rule. Anything it cannot place makes the row fail; the fix then goes
into the parser (`debug_row` shows where a row gets stuck).

## Key Directories

| Path | Purpose |
|------|---------|
| [engine/process_csv.py](engine/process_csv.py) | Normalizer entry point; orchestrates all stages. `main()` + `NORMALIZED_FIELDNAMES` |
| [engine/core/](engine/core/) | Shared pipeline modules: `csv_runtime`, `csv_validation`, `row_state`, `completion`, `runtime`; `xlsx_reader` (xlsx → rows) and `pdf_text` (PDF → pieces of text with page and position), both stdlib; `parsers` (IBAN with checksum, amounts, structured reference; for every bank and the importer) |
| [engine/banks/](engine/banks/) | One sub-package per bank. Each must export `normalize_row()` and has a `README.md` (see "Banks") |
| [engine/banks/fintro/](engine/banks/fintro/) | Reference bank: `normalize` (exports `normalize_row`), `extract_details`, `parsers`, `reconcile`; `debug_trace` (its part of `debug_row`) |
| [engine/banks/argenta/](engine/banks/argenta/) | Argenta accounts (xlsx): `normalize`, `extract_card_details`, `parsers`, `debug_trace`; the README covers the Mastercard too |
| [engine/banks/argenta_mastercard/](engine/banks/argenta_mastercard/) | Argenta Mastercard (PDF statement): `statement` (the reader), `normalize`, `parsers`, `debug_trace`, `archive_check` |
| [engine/archive_checks.py](engine/archive_checks.py) | Checks across the archived exports: runs every `engine/banks/<bank>/archive_check.py` |
| [docs/output-contract.md](docs/output-contract.md) | What other tools may rely on in the normalized output and in Firefly |
| [docs/archive-and-reload.md](docs/archive-and-reload.md) | The archive, what happens to each file and row, `start-over.bash`, setup, restore on a new server |
| [docs/adding-a-bank.md](docs/adding-a-bank.md) | How a new bank is added: questions, survey, one subagent per step, load, example prompts |
| `plans/` | Plans for work in progress; once done, their lasting facts move into the docs and the plan is deleted (git keeps it) |
| [engine/regression.py](engine/regression.py) | Regression test: every row of the bank exports through a git ref and the working tree, reporting each changed result |
| [engine/debug_row.py](engine/debug_row.py) | Why a row fails or is skipped, for every bank; a bank adds its own trace in an optional `engine/banks/<bank>/debug_trace.py` (`show_trace(row)`) |
| [engine/firefly/](engine/firefly/) | Firefly III import: `api` (REST client), `import_normalized` (importer), `accounts` (the own accounts file) |
| [deploy/](deploy/) | Source of root-side helper scripts; installed by hand on each server, never run from here (see "Root helper") |
| [config/](config/) | `<bank>.yaml` configs (bank name is the filename) + `app.env` (server only, see "Setup") + [accounts.example.yaml](config/accounts.example.yaml) (template of the own accounts file, no bank config) |
| [firefly-iii-feeder.bash](firefly-iii-feeder.bash) | Cron entry: normalizes each incoming export, imports, commits and pushes `archive/` |
| [start-over.bash](start-over.bash) | Wipes Firefly's transactions and reloads the archive (see "Start Over") |
| `archive/` | Private git repo (server only, not in this repo): every export dropped in `incoming/`, in `originals/<bank>/<account>/` (finished) or `unprocessed/` (rejected, crashed); `accounts.yaml`, the own accounts (architectural_patterns.md §14) |
| `bank-csv-originals/` | Exports from before the archive, one subfolder per bank: `Fintro/` is in the archive too, `Argenta/` (xlsx, pdf) enters it once the feeder reads those |
| `data/incoming/` | Drop exports (csv, xlsx, pdf; any case) here to trigger processing; one no bank config recognizes goes to `archive/unprocessed/` with an alert |
| `data/normalized/` | Normalized output waiting for import: `<base>-normalized[-partial].csv` |
| `data/imported/` | Normalized files after import: `<base>-imported.csv` (`-imported-partial` if any row of the bank file failed, in the normalizer or the import) |
| `data/failed-rows/` | Rows that failed normalization, dedup, or import: `<base>-normalize-failed.csv`, `-duplicate-failed.csv`, `-import-failed.csv` |
| `data/row-state/` | Per account (`<account>.csv`): every bank row the feeder knows and its Firefly id; `backups/`. See architectural_patterns.md §7 |
| `data/logs/` | `<base>.log` (normalizer) and `<base>-import.log` (mode, Firefly state read time, result and duration). `<time>-firefly-follow-up.log`: rules and running balances after a batch import, with durations; `<time>` is when the batch set the flag, so retries append. `<time>-firefly-accounts.log`: what the accounts file changed in Firefly, only when it changed something |
| `data/*.flag` | State between runs (pending follow-up or push, one alert per outage): architectural_patterns.md §11 |
| `data/temp/` | Working files (output and row state being prepared); emptied at the start and the end of each bank file |
| `data-before-start-over/` | The previous `data/`, moved aside by `start-over.bash`; replaced by the next one |

File names in `data/` describe the content: `<base>` = `<run>-<bank>-<account>-<first>_<last>`,
e.g. `20261009-101500-001-fintro-BE68539007547034-2026-03-13_2026-10-07`. `<run>` = start of the
cron run + the file's number in that run; `<first>_<last>` = the period printed on a statement,
else the first and last transaction date (`date_format` of `primary_transaction_date` in the bank
config). How they are made: architectural_patterns.md §10.

## Deploy = save

The VS Code SFTP watcher (`.vscode/sftp.json`, local) uploads every saved file at
once, so an edit runs on the server from the next cron minute — before any commit,
and also when Claude saves it. A change to the normalized columns, to file names in
`data/`, or to anything the importer reads must therefore be saved only while
`data/incoming/` and `data/normalized/` on the server are empty and no
`data/*.flag` is pending: check that first (read-only `ls`).

## Server access

AI agents only read on the server (`ls`, `cat`, `stat`: `data/` and the token live only
there). Anything that writes — moving a file, a git command, `start-over.bash`, an install — is
handed to the user as a plain command to paste. The user's shell there is root, while the cron
job and every file in the feeder folder belong to the cron user. So a command that writes in the
feeder folder runs as that user (`sudo -H -u <cron user> …`; without `-H` git looks for its config in `/root`): a root-owned file in `data/` or
`archive/` (git objects above all) breaks the next cron run.

## Running

```bash
# Manual single run (normalize data/incoming/, then import data/normalized/)
./firefly-iii-feeder.bash

# Normalizer only (debugging)
PYTHONPATH=. python3 -m engine.process_csv <file> <run> <logfile_path>   # <run>: YYYYMMDD-HHMMSS-NNN

# Why a row fails or is skipped; on the server (the row state is in data/), reads only.
# <row>: the "source row" of the normalizer log, or an external_id (also finds filtered rows)
PYTHONPATH=. python3 -m engine.debug_row <file> <row> [<row> ...]

# Importer only; --dry-run builds every request but sends and moves nothing
PYTHONPATH=. python3 -m engine.firefly.import_normalized [--dry-run] [--show N] [csv ...]

# Own accounts file only (default archive/accounts.yaml), as the cron user; --dry-run shows what
# would change. To change the file: a draft data/accounts-draft.yaml through the share, dry-run it
# with --accounts, then copy it over archive/accounts.yaml (the next run with work commits it)
PYTHONPATH=. python3 -m engine.firefly.import_normalized --accounts-only [--accounts <path>] [--dry-run]

# Checks across the archived exports (card statements against the paying account), reads only;
# --print prints the differences instead of alerting
PYTHONPATH=. python3 -m engine.archive_checks --print [--archive <dir>]
```

Alerts go to stderr ([engine/core/runtime.py](engine/core/runtime.py) `alert`);
success is silent.

## Testing

Regression test, before every parser change is committed (desktop, in the repo;
the archived originals are only read via the share; exit 1 when anything changed).
`**` is recursive, so this covers every bank and file type; a file a version cannot
read is listed with the reason, and rows only one version has are reported apart:

```bash
python -m engine.regression [--base REF] [--show N] "//<server>/firefly-iii-feeder/archive/originals/**/*"
```

Every reported change must be explained: fixed rows, and changed outputs that
only add information. Verify the normalizer end to end on the server (via the share; `data/` is not
synced) by placing a real export in `data/incoming/`
and inspecting `data/normalized/`, `data/failed-rows/`, and `data/logs/` (failure
reasons are in the normalizer log, not the `-import.log`). Use a real export: every file
dropped there is archived and pushed, and only an already archived one is dropped as a copy. Verify
the importer with `--dry-run` on the server (the token lives there).

Testing a change to `firefly-iii-feeder.bash` on the desktop (Git Bash): run it in
a throwaway copy, never in the repo's own `data/`. Git Bash has no `flock`, so a stub
stands in for it; the upload wait drops to 0 s; without `app.env` the importer ends
with "FIREFLY IMPORT BLOCKED", and without `archive/.git` every run with work alerts
"ARCHIVE NOT UNDER GIT", both as expected (to test the git step: `git init` in
`archive/` with a local bare repo as `origin`). Use the `/c/…` form for the folder: a `C:`
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

## Lint

```bash
ruff check .
pre-commit run --all-files   # what every commit runs: ruff, ruff format, shfmt, shellcheck
```
Config: [ruff.toml](ruff.toml) — line-length 120, py310 target, selects `E,F,W,I,UP,B`;
hooks in [.pre-commit-config.yaml](.pre-commit-config.yaml).

## Exit Codes

Normalizer — [engine/process_csv.py](engine/process_csv.py) and
[engine/core/completion.py](engine/core/completion.py), in `finalize` order for 92–97
(architectural_patterns.md §8):

| Code | Outcome |
|------|---------|
| `0` | success, all_full_duplicates, or all_filtered (nothing to do) |
| `1` | Python stopped before `finalize` (traceback on stderr); bash archives the original |
| `65` | structure_failed / all_failed |
| `75` | partial (some rows failed) |
| `97` | row-state update not prepared |
| `92` | normalized output not moved (nothing recorded) |
| `93` | row state not replaced (the output is taken back out of `data/normalized/`) |
| `94` | original not archived (its rows are recorded) |
| `99` | unexpected exception |

Where the original goes per outcome: [docs/archive-and-reload.md](docs/archive-and-reload.md), rule 2.

Importer — [engine/firefly/import_normalized.py](engine/firefly/import_normalized.py):
`0` all imported, `75` some rows failed, `69` Firefly unreachable, token refused, or the accounts
file invalid or refused (files stay in `data/normalized/`), `70` unexpected crash (files stay).
Alerted once per outage or crash through flags (architectural_patterns.md §11). With
`--accounts-only`: `0` applied, `69` the same blocks (printed, no flag), `2` no file.

## Banks

Everything specific to one bank — how to export, its columns, what makes a row
unique, parsing rules, what is dropped on purpose, markers, quirks — is in that
bank's README. Read it before changing the bank's config or module. Claude Code
loads it by itself when working in the bank's folder (`CLAUDE.md` there imports it).

| Bank | Export | Docs |
|------|--------|------|
| Fintro | CSV, one per account | [engine/banks/fintro/README.md](engine/banks/fintro/README.md) |
| Argenta | xlsx, one per account; Mastercard: monthly PDF statement | [engine/banks/argenta/README.md](engine/banks/argenta/README.md) |

## Firefly III Import

How rows become transactions (modes, transfers, failures): architectural_patterns.md §13;
the field mapping: [docs/output-contract.md](docs/output-contract.md). Gotchas:

- Firefly's **duplicate hash covers every field** of the split (SHA-256 of the
  whole request row, absent fields as null; only `import_hash_v2`, `original_source`
  and `batch_submission` are left out).
  So a change to what `build_split` sends makes an already imported row a second
  transaction when it is sent again, if nothing else stopped it. The row state does:
  a row whose `firefly_id` is set is never sent again, and a re-dropped export
  stops at the row state before the importer. To give rows already in Firefly the
  new fields: Start Over.
- Firefly's duplicate check includes **deleted** transactions. After deleting
  transactions, also `DELETE /api/v1/data/purge`, or a re-import silently
  counts them as "already present".
- `DELETE /api/v1/data/destroy` of many objects answers 504 after about a
  minute and stops partway: repeat it until 204, then purge. Destroying
  transactions does not delete the expense/revenue accounts they created
  (`objects=expense_accounts` / `revenue_accounts`).
- Dates must be ISO; Firefly parses `dd/mm/yyyy` as US month/day without error.
- An own account's IBAN may not be on any other asset, expense or revenue account (Firefly's
  `UniqueIban`; the account number likewise). Expense and revenue accounts that earlier imports
  made for an IBAN therefore make Firefly refuse that IBAN as a new own account: start over, which
  removes them before the first run applies the accounts file.

## Setup (by hand, once per server)

1. **Firefly III ≥ 6.7.0.** Before it (issue #12710) the duplicate hash included the batch flag,
   so a row imported per row and again in batch mode was stored twice.
2. **`enable_batch_processing` on**, in Firefly's admin configuration in the GUI: the
   configuration API stores it as text, which Firefly ignores. While it is off, batch mode
   silently does the full per-row work.
3. **Default currency EUR** in Firefly.
4. **Own (asset) accounts**: `archive/accounts.yaml` in the archive repo (step 8), a copy of
   [config/accounts.example.yaml](config/accounts.example.yaml) filled in, dry-run first ("Running").
   The first run that imports creates them in Firefly. Every account needs its IBAN or account
   number: without one, every row of that account fails. A credit card with a statement also
   needs `paid_from`, the account that pays it off.
5. **`config/app.env`** on the server, mode 600, owned by the cron user: `FIREFLY_URL` and
   `FIREFLY_TOKEN` (a Personal Access Token of Firefly's owner, the only user who may create a
   currency Firefly doesn't know yet; it expires after at most one year → "FIREFLY IMPORT
   BLOCKED" alert). The SFTP watcher excludes the file, so a local copy is never uploaded over it.
6. **Feeder folder** owned by the cron user.
7. **Root helper and its sudo rule** (below).
8. **Archive repo**: [docs/archive-and-reload.md](docs/archive-and-reload.md), "Setup".
9. **Cron job** in the TrueNAS GUI: every minute, as the user that owns the folder, "Hide
   Standard Error" off (stderr = alert email), with the encrypted-dataset lock-guard in the
   Command field: `if test -e <path>; then bash <path>; fi`. `bash` in front, because every SFTP
   upload of the script drops its execute bit, and a script cron cannot execute fails silently.

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

The normal way to apply a structural change to what is already in Firefly
(hard rule "Rebuild from scratch"). Rows already in the row state never
reach the parser again, so a parser change only affects new rows; to give
already imported transactions the new output too, start over. Not needed for
a renamed bank column (add the name to `names`) or a changed `columns.required`
(the row state adapts, see architectural_patterns.md §7).

Run the regression test first (desktop). The own accounts stay, and the first run sets them to
`archive/accounts.yaml`; `--wipe-accounts` deletes them too, and the first run creates them again
from the file. The command, what it does and what it does not restore:
[docs/archive-and-reload.md](docs/archive-and-reload.md), "`start-over.bash`".

## Adding a New Bank

The way of working (questions, survey, one subagent per step, load, example prompts):
[docs/adding-a-bank.md](docs/adding-a-bank.md). The code-level steps:

1. Create `config/<bank>.yaml` — required/optional columns, regex rules,
   filter values, `duplicate_key`, and `date_format` on `primary_transaction_date`
   (else the file names carry no period). See [config/fintro.yaml](config/fintro.yaml).
   Bank name is derived from the filename by `load_all_bank_configs()`
   in [engine/core/csv_runtime.py](engine/core/csv_runtime.py) — no `bank:` field needed; a
   `*.example.yaml` is a template, never a bank.
2. Create the package `engine/banks/<bank>/`, exposing `normalize_row` in
   `__init__.py`. See [engine/banks/fintro/](engine/banks/fintro/).
   - A CSV or xlsx needs no reader: `load_rows` reads its table by extension. A PDF does: name a
     module of the package in the yaml (`reader: statement`) whose `read(path)` returns
     `(rows, source_lines, period)`, or `None` for a file that is not its own. `rows` are dicts
     under the reader's own column names (the yaml's `names`), `source_lines` one text per row
     naming where it is in the file (`p3/12`, for the logs and `debug_row`), `period` the
     `(first, last)` ISO dates the file prints, else `None`. Every reader is offered every PDF;
     exactly one may claim it. See
     [engine/banks/argenta_mastercard/statement.py](engine/banks/argenta_mastercard/statement.py).
   - Optional `archive_check.py` with `check_archive(archive_dir) -> list[str]` (one text per
     difference), for what only several archived exports together show;
     [engine/archive_checks.py](engine/archive_checks.py) finds and runs it.
3. Document it in `engine/banks/<bank>/README.md` with the headings of the Fintro
   README (Export, Columns, Unique row, Field mapping, parsing, Dropped on purpose,
   Markers and conventions, Quirks, Checking), add `engine/banks/<bank>/CLAUDE.md`
   containing `@README.md`, a row in "Banks" above and in README.md, and its
   transaction types and unique key in [docs/output-contract.md](docs/output-contract.md).
4. No further code changes — `autodetect_bank()` in
   [engine/core/csv_validation.py](engine/core/csv_validation.py) matches configs by
   header, and `process_csv.py` imports the bank module dynamically via
   `importlib.import_module(f"engine.banks.{bank_name}")`. The importer needs
   no change either, as long as the account is in Firefly (through the accounts file) with its
   IBAN or account number.
