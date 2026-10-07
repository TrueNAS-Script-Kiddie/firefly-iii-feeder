---
paths:
  - engine/**
  - config/**
  - bank-csv-normalizer.bash
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
4. **Resolve account-specific dedup-index path** — from `duplicate_key.partition_by`.
5. **Load dedup index** — per-account persistent CSV
   (`load_duplicate_index` in [engine/core/duplicate_index.py](../../engine/core/duplicate_index.py)).
6. **Per-row loop** — `extract_duplicate_key` → `classify_duplicate` → bank's
   `normalize_row` → write temp output.
7. **Outcome classification** — `success` / `partial` / `all_failed` /
   `all_full_duplicates` / `all_filtered` / `structure_failed` / `error`.
8. **Finalize** — single exit path for all file moves, index commit, alert
   (`finalize` in [engine/core/completion.py](../../engine/core/completion.py)).

## 2. Single Exit Path (`completion.finalize`)

`finalize()` is the **only** place that calls `sys.exit()` (apart from the
argument check at the top of `main()`). Every code path
(normal, error, structure failure, row-level failure) calls it, guaranteeing
that file moves, index commits, writer cleanup, backup rotation, temp cleanup,
and the failure alert always happen together. Outcomes map 1:1 to exit codes and
destination subdirectories — see the step-numbered blocks inside `finalize`.

## 3. Configuration-Driven Bank Support

Bank-specific behaviour lives in `config/<bank>.yaml` and
`engine/banks/<bank>/`. `engine/core/` has zero hardcoded bank names. Key YAML sections (see [config/fintro.yaml](../../config/fintro.yaml)):

- `columns.required` — expected headers with `names` (aliases), `regex`
  (per-cell validation), `filter` (exact-match allowlist), `filter_regex`.
- `columns.optional` — headers included when present, ignored when absent.
- `duplicate_key.columns` / `duplicate_key.regex` — how to extract the dedup
  key from a row.
- `duplicate_key.partition_by` — internal column whose value names the
  per-account index file (`<VALUE>-duplicate-index.csv`). Falls back to
  `<bank>-duplicate-index.csv` if absent.

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

Fintro CSVs carry many fields in both a dedicated column and inside the
free-text `details` column. The normalizer compares both sources and either
merges them or raises `ValueError` on mismatch — neither source is blindly
trusted.

Precedence rules live in [engine/banks/fintro/normalize_row.py](../../engine/banks/fintro/normalize_row.py):

- **CSV wins** for amount, IBAN, dates, and free-text messages (details
  validates).
- **Details wins** for structured references, BIC, and fields missing from
  the CSV column.
- **Structured messages** (Belgian `+++xxx/xxxx/xxxxx+++` format) take
  priority over free-text when either source has one
  (`extract_structured_ref` in [engine/banks/fintro/parsers.py](../../engine/banks/fintro/parsers.py)).

`merge_opposing_account_name` and `reconcile_transaction_types` in
[engine/banks/fintro/reconcile.py](../../engine/banks/fintro/reconcile.py) encode the
field-by-field rules. The counterparty name is the column name plus whatever the
details add after it (often the address); a leading filler `VAN` is dropped only
when the column name proves it is not part of the name (`VAN DER MEULEN TOM`).
Both follow the normalization principle in AGENTS.md: nothing meaningful is lost.

## 6. Two-Phase `normalize_row` + Sequential `details` Parsing

`normalize_row()` in [engine/banks/fintro/normalize_row.py](../../engine/banks/fintro/normalize_row.py)
is split into two explicit phases:

**Phase 1 — Extraction.** Pull all values into named variables; no output is
written yet.
- *1a* — individual dedicated columns are pulled by key from `csv_row`.
- *1b* — `extract_details()` in [engine/banks/fintro/extract_details.py](../../engine/banks/fintro/extract_details.py)
  parses the free-text `details` column in two sub-passes:
  1. Easy-to-detect postfixes anchored with `$` are stripped first
     (VALUTADATUM, BANKREFERENTIE, UITGEVOERD OP).
  2. The remainder is matched by leading pattern anchored with `^` to
     identify transaction type and extract the rest (VERBETERING, STORTING, DOORLOPENDE
     OPDRACHT, DOMICILIERING, BUITENLANDSE OVERSCHRIJVING, OVERSCHRIJVING,
     BETALING, ANNULERING BETALING, MOBIELE BETALING, GELDOPNEMING, old-card
     fallback).

  Each matched segment is removed from `remaining_details`. Anything left at
  the end raises `ValueError`.

**Phase 2 — Reconcile, reformat, assemble.** No parsing of `details` at this
stage; only cross-source decisions, cosmetic replacement (via the
`REPLACE_IN_*` tables in `normalize_row.py`), card-number masking and
exchange-cost formatting (two small regexes), and final
assembly of the 18 `NORMALIZED_FIELDNAMES` defined in
[engine/process_csv.py](../../engine/process_csv.py).

## 7. Stateful In-Memory + Persistent Dedup Index

The dedup index is a `defaultdict[str, list[dict]]` loaded from a per-account
CSV at the start of each run. A row enters the index only after
`normalize_row` succeeded, so failed rows are retried when their CSV is
processed again. New rows are appended to the in-memory dict during the loop so
that intra-batch duplicates are caught before the index is committed, which
happens only for `success` / `partial` runs.

The index filename is `<partition_value>-duplicate-index.csv` (e.g.
`BE12345678901234-duplicate-index.csv`), derived from
`duplicate_key.partition_by` in the YAML (see pattern 3). If `partition_by`
is absent, it falls back to `<bank>-duplicate-index.csv`.

`classify_duplicate()` in [engine/core/duplicate_index.py](../../engine/core/duplicate_index.py)
returns:
- `new` — key not seen.
- `identical` — key seen, all required fields match → silently skip.
- `conflict` — key seen, required fields differ → write to duplicate-failed.

The index columns are `duplicate_key` + `columns.required`. When the config's
required columns change, the next commit rewrites the index with the new
columns (logged): a removed column is dropped, an added one is `<not recorded>`
(`NOT_RECORDED`) for the old rows and skipped when comparing them, so no
re-import is needed.

The index is committed at the end: snapshot (timestamped copy in
`backups/`) → atomic copy-to-live (temp file + `os.replace`, `copy_atomically`)
→ rotate backups (`rotate_duplicate_backups`, per account: at most
`MAX_BACKUPS=50`, none older than `MAX_BACKUP_AGE_DAYS=365`, newest always kept). A rollback copy
(`previous-duplicate-index.csv` in `data/temp/`) allows recovery if the
normalized-output move fails — see step 4 of `finalize`.

## 8. Tiered Error Criticality

The step-numbered blocks inside `finalize()` in
[engine/core/completion.py](../../engine/core/completion.py) distinguish:

- **Non-critical** (wrap in try/except, log, continue): backup rotation,
  temp cleanup, logging itself.
- **Critical** (attempt compensating move, alert, exit): original CSV move
  (exit 94), duplicate-index commit (93), normalized output move (92),
  duplicate-index prep (97).
- **Catastrophic** (exit 99): unhandled exception anywhere in the pipeline.

Non-critical failures never interrupt the happy path. Critical failures
always attempt a compensating action (move processed file to failed,
roll back index from `previous-duplicate-index.csv`) before exiting.

## 9. Context Dict as Pipeline State

A single `context` dict is threaded through all phases and into `finalize()`.
It carries `paths`, `open_writers`, `duplicate_index_rows_to_add`,
`log_event`, and the run timestamp. This avoids module-global state while
keeping the pipeline callable as a unit — see `main()` in
[engine/process_csv.py](../../engine/process_csv.py).

## 10. Timestamp-Everything Convention

All normalized output, processed originals, failed rows, logs, and
duplicate-index backups include the run timestamp `YYYYMMDD-HHMMSS` in the
filename. The importer reuses it (`<ts>-<name>-imported.csv`,
`<ts>-<name>-import.log`), so all files of one bank CSV sort together. This makes concurrent runs distinguishable and provides a
complete audit trail without a database.

Format constant `RUN_TS_FORMAT` is defined in
[engine/core/duplicate_index.py](../../engine/core/duplicate_index.py); generated by
[bank-csv-normalizer.bash](../../bank-csv-normalizer.bash) (`date '+%Y%m%d-%H%M%S'`)
and consumed by `build_paths` in [engine/core/csv_runtime.py](../../engine/core/csv_runtime.py).

## 11. Cron-Safe Orchestration

[bank-csv-normalizer.bash](../../bank-csv-normalizer.bash) guards unattended execution:

- Sets `PYTHONPATH` and `cd`s into `BASE_DIR` (cron has no defaults).
- Exclusive `flock` on `.process.lock` for the whole run (normalizer and
  importer); a second instance exits at once, and the kernel drops the lock
  when the process dies, so a crash or reboot cannot leave a stale lock.
- **Upload check** — skip a file until its `ctime` is ≥ 30 s old (Explorer and
  `cp -p` keep an old mtime, but every write bumps ctime) and its last byte is a
  newline; after 10 min process anyway so a broken file gets reported.
- **Idle runs** exit before the lock when `incoming/` and `normalized/` hold no
  CSV and no `firefly-recalculate.flag` is pending: builtins only, no Python.
- Exit codes `0/65/75/99` are "Python handled it"; anything else triggers a
  fallback move of the incoming file to `data/failed/`.
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
  row "already present" (needs Firefly ≥ 6.7.0: older versions hashed the batch
  flag, so the same row in per-row and batch mode was stored twice). Firefly's check includes deleted transactions:
  purge (`DELETE /api/v1/data/purge`) after deleting, before re-importing.
- **Firefly state read per run** — asset accounts by IBAN, existing transfers
  for matching. Nothing about Firefly is configured in this repo.
- **Own transfers: match or create** — both accounts' CSVs carry the same
  movement; the first side creates the transfer, the other claims it
  (`claim_transfer`: same accounts, amount, ±`TRANSFER_MATCH_DAYS`, one claim
  per side). Robust to file order and unequal history coverage.
- **Run-level vs row-level failure** — `FireflyAuthError` /
  `FireflyUnavailableError` ([engine/firefly/api.py](../../engine/firefly/api.py))
  stop the run and leave files in place, alerted once per outage via
  `data/firefly-import-blocked.flag`; any other rejection fails only that row
  (to `data/failed/<ts>-<name>-import-failed.csv`) and the file moves to
  `data/imported/` as `<ts>-<name>-imported-partial.csv`. A file the normalizer
  already marked `-normalized-partial` also ends up `-imported-partial`. Retry:
  move the `-import-failed.csv` into `data/normalized/` (→ `-imported-retry`).
  An unexpected exception (a bug) also leaves the files in place: its traceback
  is alerted once via `data/firefly-import-crashed.flag` (exit 70).
- **Mode per run** — per row for recent data, batch (`batch_submission`) when
  more than `BATCH_OLD_ROWS_THRESHOLD` rows are older than
  `BATCH_OLD_ROW_AGE_DAYS`, relative to the day of the run. Per row, Firefly
  recalculates every later balance of the account, so old rows cost seconds each;
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
