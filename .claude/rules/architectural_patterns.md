---
paths:
  - engine/**
  - config/**
  - firefly-iii-feeder.bash
---

# Architectural Patterns

## 1. Linear Multi-Phase Pipeline

[engine/process_csv.py](../../engine/process_csv.py) `main()` runs a strict
ordered sequence. Each phase must succeed before the next begins:

1. **Load** — encoding/delimiter auto-detection (`load_csv_rows` in
   [engine/core/csv_runtime.py](../../engine/core/csv_runtime.py)).
2. **Autodetect bank** — match CSV headers against all loaded YAML configs
   (`autodetect_bank` in [engine/core/csv_validation.py](../../engine/core/csv_validation.py)).
3. **Validate & map** — remap CSV columns to internal names, apply `filter` /
   `filter_regex` (dropped rows are counted and logged per reason), then `regex`
   validation (`validate_and_prepare`). A row with a failing cell is marked, not
   fatal: it goes to normalize-failed in the loop. Only all rows failing, or a
   file mixing several `partition_by` values, rejects the file (exit 65). All
   rows filtered is `all_filtered`: nothing to do, exit 0.
4. **Resolve the account's row-state path** — from `duplicate_key.partition_by`.
5. **Load row state** — per-account persistent CSV
   (`load_row_state` in [engine/core/row_state.py](../../engine/core/row_state.py)).
6. **Per-row loop** — `extract_duplicate_key` → `classify_duplicate` → bank's
   `normalize_row` → write temp output.
7. **Outcome classification** — `success` / `partial` / `all_failed` /
   `all_full_duplicates` / `all_filtered` / `structure_failed` / `error`.
8. **Finalize** — single exit path for the output move, row-state commit, archiving the original, alert
   (`finalize` in [engine/core/completion.py](../../engine/core/completion.py)).

## 2. Single Exit Path (`completion.finalize`)

`finalize()` is the **only** place that calls `sys.exit()` (apart from the
argument check at the top of `main()`). Every code path
(normal, error, structure failure, row-level failure) calls it, guaranteeing
that the output move, row-state commit, archiving the original, writer cleanup, backup
rotation, temp cleanup, and the failure alert always happen together. Outcomes map
1:1 to exit codes and destinations: a finished outcome archives the original in
`archive/originals/`, any other in `archive/unprocessed/`
([docs/archive-and-reload.md](../../docs/archive-and-reload.md)). See the
step-numbered blocks inside `finalize`.

## 3. Configuration-Driven Bank Support

Bank-specific behaviour lives in `config/<bank>.yaml` and
`engine/banks/<bank>/`. `engine/core/` has zero hardcoded bank names. Key YAML sections (see [config/fintro.yaml](../../config/fintro.yaml)):

- `columns.required` — expected headers with `names` (aliases), `regex`
  (per-cell validation), `filter` (exact-match allowlist), `filter_regex`.
- `columns.optional` — headers included when present, ignored when absent.
- `duplicate_key.columns` / `duplicate_key.regex` — how to extract the dedup
  key from a row.
- `duplicate_key.partition_by` — internal column whose value names the
  account: its row-state file (`<VALUE>.csv`) and the first part of `row_key`.
  Falls back to the bank name if absent.

Bank name is derived from the YAML filename and injected as `cfg["bank"]` by
`load_all_bank_configs` in [engine/core/csv_runtime.py](../../engine/core/csv_runtime.py).
The normalizer module is loaded dynamically by `process_csv.py` via
`importlib.import_module(f"engine.banks.{bank_name}")`.

## 4. Lazy Writer Pattern

CSV writers are never opened speculatively. Each output file
(`temp_normalized`, `failed_normalize`, `failed_duplicate`) holds a
`{"writer": None, "file": None}` ref dict. `ensure_writer()` in
[engine/core/csv_runtime.py](../../engine/core/csv_runtime.py) creates the file +
writer on first use and writes the header once.

All refs are collected in `context["open_writers"]` for guaranteed cleanup by
`close_open_writers()` in [engine/core/completion.py](../../engine/core/completion.py).

## 5. Two-Source Data Reconciliation

When a bank states the same fact twice (a dedicated column and a free-text
field), its module compares both and either merges them or raises `ValueError`
on a mismatch — neither source is blindly trusted. Which source wins per field
is bank-specific and documented in the bank's README (Fintro:
[engine/banks/fintro/README.md](../../engine/banks/fintro/README.md), "Two sources").
Both follow the normalization principle in AGENTS.md: nothing meaningful is lost.

## 6. Two-Phase `normalize_row` + Sequential Free-Text Parsing

A bank's `normalize_row()` is split into two explicit phases:

**Phase 1 — Extraction.** Pull all values into named variables; no output is
written yet: dedicated columns by key from `csv_row`, then the free-text field.
Free text is parsed sequentially and destructively: each matched segment is
cut from the remaining text, so a later pattern cannot match it again, and
anything left at the end raises `ValueError`.

**Phase 2 — Reconcile, reformat, assemble.** No parsing at this stage; only
cross-source decisions, cosmetic replacement, and final assembly of the
`NORMALIZED_FIELDNAMES` defined in [engine/process_csv.py](../../engine/process_csv.py)
(all but `row_key`, which `process_csv` adds).

The patterns and their order are bank-specific: see the bank's README (Fintro:
"Parsing `Details`").

## 7. Row State: Dedup Plus Firefly Id

[engine/core/row_state.py](../../engine/core/row_state.py). One CSV per account,
`data/row-state/<account>.csv` (`<account>` = the `duplicate_key.partition_by`
value, else the bank name; the same as the first part of `row_key`), with
`duplicate_key`, `firefly_id` and the `columns.required` values of every bank row
the feeder knows.

- **The normalizer** loads it as a `defaultdict[str, list[dict]]` at the start of
  each file. A row enters it only after `normalize_row` succeeded, so failed rows
  are retried when their CSV is processed again. New rows are appended to the
  in-memory dict during the loop, so intra-batch duplicates are caught too;
  `finalize` commits the new rows (`firefly_id` empty) only for `success` /
  `partial`.
- **The importer** (`record_import`, per file, before the file moves) fills
  `firefly_id`: the Firefly transaction the row created, or the transfer it
  claimed, or the one Firefly named as duplicate. A row Firefly refused is removed
  again, so the next export holding it retries it. A row whose `firefly_id` is set
  is never sent again.
- **Transfer sides**: a transfer's side is used when a row of that account has its
  id as `firefly_id` (`load_transfer_pool`), so a side is claimed at most once ever,
  across runs (§13).

`classify_duplicate()` returns:
- `new` — key not seen.
- `identical` — key seen, all required fields match → silently skip.
- `conflict` — key seen, required fields differ → write to duplicate-failed.

When the config's required columns change, the next commit rewrites the state with
the new columns (logged): a removed column is dropped, an added one is
`<not recorded>` (`NOT_RECORDED`) for the old rows and skipped when comparing them,
so no re-import is needed.

Commit (`finalize` step 3): the updated state is prepared in `data/temp/`
(`create_updated_row_state`), replaces the live file atomically (temp file +
`os.replace`, `copy_atomically`), then a copy goes to `backups/<run>-<account>.csv`
(`rotate_backups`, per account: at most `MAX_BACKUPS=50`, none older than
`MAX_BACKUP_AGE_DAYS=365`, newest always kept). The importer's updates replace the
file the same way, without a backup.

## 8. Tiered Error Criticality and Finalize Order

`finalize()` in [engine/core/completion.py](../../engine/core/completion.py) runs:
1. prepare the updated row state in `data/temp/` (critical, 97);
2. move the normalized output to `data/normalized/` (critical, 92);
3. replace the live row state, then back it up (critical, 93; backup not critical);
4. archive the original (critical, 94), last;
5. remove a finished original's copies from `archive/unprocessed/`; rotate backups;
   clean `data/temp/` (all not critical).

Until step 4 the original stays in `data/incoming/`, so a run interrupted hard
(reboot, kill) is simply redone by the next one: after step 2 the output is made
twice and the importer skips the rows already recorded; after step 3 every row is
`identical` and the original is archived.

- **Non-critical** (wrap in try/except, log, continue): removing a finished
  original's copies from `archive/unprocessed/`, backup and rotation, temp cleanup,
  logging itself.
- **Critical** (compensate, alert, exit): the original goes to
  `archive/unprocessed/`, so it is not retried every minute and can be dropped in
  again. At 93 the output is also taken out of `data/normalized/` again (the
  importer runs only after every bank file, so it has not seen it). At 94 the rows
  are already recorded; dropped in again, the original is `identical` and archived.
- **Catastrophic** (exit 99): unhandled exception anywhere in the pipeline.

## 9. Context Dict as Pipeline State

A single `context` dict is threaded through all phases and into `finalize()`.
It carries `paths`, `open_writers`, `row_state_rows_to_add`,
`log_event`, and the run id. This avoids module-global state while
keeping the pipeline callable as a unit — see `main()` in
[engine/process_csv.py](../../engine/process_csv.py).

## 10. Content-Describing File Names

Every file of one bank file in `data/` — normalized output, failed rows, logs,
import outputs — is named `<base>-<stage>`, with
`<base>` = `<run>-<bank>-<account>-<first>_<last>` (format in AGENTS.md, Key
Directories). All files of one bank file sort together, and a name says what is
in it; this gives a complete audit trail without a database.

- **Unique by construction**: `<run>` = start of the cron run
  (`YYYYMMDD-HHMMSS`, `RUN_TS_FORMAT` in
  [engine/core/row_state.py](../../engine/core/row_state.py)) + the
  file's number in that run, generated by
  [firefly-iii-feeder.bash](../../firefly-iii-feeder.bash) with builtins. A run
  starting in a second already used by an earlier run leaves the work to the
  next minute.
- **Python names, bash only numbers**: `build_paths` in
  [engine/core/csv_runtime.py](../../engine/core/csv_runtime.py) starts with
  `<run>-unknown-<source name>`; once bank, account and period are known
  (before the row loop) `process_csv` sets the final names, and `finalize`
  renames the log `<run>.log` that bash created.
- **The importer** derives its names (`-imported`, `-import.log`,
  `-import-failed`) from the normalized file, so they follow automatically.
- **The archived original** keeps the run id of its first arrival:
  `archive/originals/<bank>/<account>/<run>-<first>_<last>-<sha8>.<ext>` or
  `archive/unprocessed/<run>-<source name>`; a source name that already starts
  with a run id (a copy from the archive) keeps it (`ARRIVAL_RUN_ID`, in
  `csv_runtime` and in bash). So a reload processes the originals in the order
  they first arrived.

## 11. Cron-Safe Orchestration

[firefly-iii-feeder.bash](../../firefly-iii-feeder.bash) guards unattended execution:

- Sets `PYTHONPATH` and `cd`s into `BASE_DIR` (cron has no defaults).
- Exclusive `flock` on `.process.lock` for the whole run (normalizer and
  importer); a second instance exits at once, and the kernel drops the lock
  when the process dies, so a crash or reboot cannot leave a stale lock.
- **Upload check** — skip a file until its `ctime` is ≥ 30 s old (Explorer and
  `cp -p` keep an old mtime, but every write bumps ctime) and its last byte is a
  newline; after 10 min process anyway so a broken file gets reported.
- **Idle runs** exit before the lock when `incoming/` and `normalized/` hold no
  CSV and neither `firefly-recalculate.flag` nor `archive-push-pending.flag` is
  pending: builtins only — no subshell, no external command, no Python, no write
  (hard rule, see AGENTS.md "Idle cost"). Every CSV glob skips Excel's `~$` lock
  files.
- Exit codes `0/65/75/99` are "Python handled it"; anything else triggers a
  fallback move of the incoming file to `archive/unprocessed/` (`<run>-<source
  name>`), unless that folder already holds its content.
- **Archive to git** after the importer, still under the lock, in every run with
  work: `git add -A` in `archive/`, a commit when anything changed, a push. A
  failed push sets `archive-push-pending.flag`, alerts once, and every next run
  pushes again.
- Runs every minute; alerts go to stderr, which the TrueNAS cron job emails.
  Success is silent.

## 12. Naming Conventions

All code uses explicit, descriptive English names. Related values share
prefixes to make the origin obvious in reconciliation blocks —
`column_*` for values pulled from dedicated CSV columns, `details_*` for
values extracted from the free-text `details` column, `normalized_*` for
final output fields. Names reflect meaning and purpose; no abbreviations
except common ones (IBAN, BIC, CSV).

## 13. Firefly III Import Stage

[engine/firefly/import_normalized.py](../../engine/firefly/import_normalized.py)
runs after the normalizer loop and turns every row in `data/normalized/` into
one `POST /api/v1/transactions`.

- **Idempotent** — `error_if_duplicate_hash` makes a re-import of the same
  row "already present", as long as every field sent is the same: the hash
  covers them all (needs Firefly ≥ 6.7.0: older versions hashed the batch
  flag, so the same row in per-row and batch mode was stored twice). Firefly's check includes deleted transactions:
  purge (`DELETE /api/v1/data/purge`) after deleting, before re-importing.
- **Firefly state read per run** — asset accounts by IBAN and account number,
  currencies (a disabled one a row uses is enabled), existing transfers
  for matching. Nothing about Firefly is configured in this repo.
- **Own transfers: match or create** — both accounts' CSVs carry the same
  movement; the first side creates the transfer, the other claims it
  (`claim_transfer`: same accounts, amount, ±`TRANSFER_MATCH_DAYS`, one claim
  per side, ever: the row state records which row used which transfer, §7).
  Robust to file order and unequal history coverage. The claiming side's own
  fields (`external_id`, `row_key`, notes) are not added to the transfer.
- **Run-level vs row-level failure** — `FireflyAuthError` /
  `FireflyUnavailableError` ([engine/firefly/api.py](../../engine/firefly/api.py))
  stop the run and leave files in place, alerted once per outage via
  `data/firefly-import-blocked.flag`. A 5xx on a POST is first checked with
  `GET about` (`firefly_answers`): if Firefly answers, the row is the problem and
  fails like any other rejection, so it cannot block every run; the third such row
  in a row (`ROW_5XX_OUTAGE_STREAK`, counted across the run's files) is an outage
  after all. Any other
  rejection fails only that row
  (to `data/failed-rows/<base>-import-failed.csv`) and the file moves to
  `data/imported/` as `<base>-imported-partial.csv`. A file the normalizer
  already marked `-normalized-partial` also ends up `-imported-partial`. A failed
  row leaves the row state again: the next export holding it retries it.
  An unexpected exception (a bug) also leaves the files in place: its traceback
  is alerted once via `data/firefly-import-crashed.flag` (exit 70).
- **Mode per run** — per row for recent data, batch (`batch_submission`) when
  more than `BATCH_OLD_ROWS_THRESHOLD` rows are older than
  `BATCH_OLD_ROW_AGE_DAYS`, relative to the day of the run. Per row, Firefly
  recalculates every later balance of the account, so `import_file` sends each
  file oldest first (failures keep file order), and old rows cost seconds each;
  batch costs a fixed follow-up instead (rules via `batch/finish`, balances via
  the root helper `deploy/firefly-refresh-running-balance.bash` through a sudo
  rule: Firefly has no API for it). `firefly-recalculate.flag` is set before the
  first batch row and removed only after the follow-up succeeded; a failure
  alerts once (`firefly-follow-up-alerted.flag`) and every next run retries
  silently, also when there are no new files. A slow
  `batch/finish` is not an outage (own long timeout, no block flag).
- **Dry run** — `--dry-run` runs the same decisions (including simulated
  transfer claims) without sending or moving anything.
- **Cash withdrawals** — the bank module marks them with `is_cash_withdrawal`
  (`1`); the importer books a marked row without counterparty on Firefly's Cash
  account (`build_split`). A new bank only has to set the same mark; unmarked
  rows without counterparty get `(onbekend)`.
