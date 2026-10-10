# Firefly III Feeder

Automated pipeline for ingesting bank-exported CSVs, validating them,
normalizing transactions into a unified model, deduplicating against a
persistent per-account index, and importing the result into Firefly III.
Designed for unattended cron execution on TrueNAS or any Linux host.

## Overview

- Detects incoming CSVs in `data/incoming/`
- Auto-detects the bank by matching CSV headers against YAML configs
- Validates, filters and normalizes each row
- Deduplicates via a per-account persistent index
- Emits normalized output to `data/normalized/`
- Imports `data/normalized/` into Firefly III via its REST API, then moves
  each file to `data/imported/`
- Keeps every export it received in `archive/`, a private git repo pushed to
  its own remote, so Firefly can always be wiped and reloaded with one command
  (`start-over.bash`; see [docs/archive-and-reload.md](docs/archive-and-reload.md))
- Alerts on stderr (emailed by cron); silent on success

## Supported Banks

| Bank | Export | Docs |
|------|--------|------|
| Fintro | CSV, one per account | [engine/banks/fintro/README.md](engine/banks/fintro/README.md) |

What other tools may rely on in the output: [docs/output-contract.md](docs/output-contract.md).

## Project Structure

```
firefly-iii-feeder/
├── firefly-iii-feeder.bash       # Cron entry (flock, upload check, archive commit)
├── start-over.bash                # Wipe the feeder's part of Firefly, reload the archive
├── engine/
│   ├── process_csv.py             # Normalizer entry point
│   ├── core/                      # csv_runtime, csv_validation,
│   │                              # duplicate_index, completion, runtime
│   ├── banks/
│   │   └── fintro/                # Per-bank package: README (export, parsing),
│   │                              # normalize, extract_details, parsers, reconcile
│   └── firefly/                   # api (REST client), import_normalized
├── docs/
│   ├── output-contract.md         # What other tools may rely on
│   └── archive-and-reload.md      # The archive, start-over.bash, setup, restore
├── plans/                         # Work in progress; removed once done
├── config/
│   ├── fintro.yaml                # Per-bank config (columns, regex, dedup)
│   └── app.env                    # FIREFLY_URL, FIREFLY_TOKEN (server only)
├── deploy/                        # Root helper script (installed by hand, see AGENTS.md)
├── archive/                       # Private git repo (server only): every export received
│   ├── originals/<bank>/<account>/
│   └── unprocessed/               # Rejected or crashed
├── bank-csv-originals/            # Exports from before the archive
├── data/                          # Disposable: remade by a reload
│   ├── incoming/ normalized/ imported/ failed-rows/
│   ├── duplicate-index/           # Per-account index + rotated backups
│   ├── logs/ temp/
├── ruff.toml
└── .vscode/sftp.json              # Optional auto-sync to remote host
```

## How It Works

1. Bash script runs (cron or manually). With no CSV in `incoming/` or
   `normalized/`, no pending `data/firefly-recalculate.flag` (rules and
   running balances still due after a large import) and no pending
   `data/archive-push-pending.flag` it exits at once (builtins only);
   otherwise it takes an exclusive `flock`, and a second instance exits
   immediately.
2. For each CSV in `data/incoming/`:
   - Skips a file until nothing has touched it for 30 s (its `ctime`, which
     copies with a preserved date still bump) and its last line is complete;
     the next cron run retries. Guards against half-copied uploads.
   - Numbers the file within the run (`<run>` = run start + number) and
     invokes `python3 -m engine.process_csv <csv> <run> <logfile>`.
3. The Python engine loads the CSV, auto-detects the bank, validates and
   maps columns, names every output after bank, account and period of the
   transactions (`<run>-<bank>-<account>-<first>_<last>-<stage>`), loads the
   account-specific duplicate index, and processes each row: dedup →
   normalize → write temp output.
4. A single exit path (`completion.finalize`) archives the original
   (`archive/originals/` when finished, else `archive/unprocessed/`; a copy
   already archived is dropped), commits the updated duplicate index, moves the
   normalized output, rotates
   backups, cleans the temp dir, logs, alerts on failure, and exits with an
   outcome code (`0`, `65`, `75`, `92`/`93`/`94`/`97`, `99`; see `AGENTS.md` →
   "Exit Codes").
5. After all incoming files, still under the lock, the importer
   (`engine.firefly.import_normalized`) sends every row in `data/normalized/`
   to Firefly III, one API call per transaction, and moves each
   file to `data/imported/`. Failed rows go to `data/failed-rows/`. Firefly being
   down or refusing the token blocks the import (one alert per outage) and
   leaves the files for the next run. See `AGENTS.md` → "Firefly III Import"
   for the mapping rules and gotchas.
6. Still under the lock, bash commits everything new in `archive/` and pushes
   it; a failed push is retried every minute, with one alert per outage.

## Requirements

- Python 3.10+
- `pyyaml` (all other runtime deps are stdlib)
- Bash, `stat`, `tail`, `mv`, `cmp`, `flock`, `git`; `curl` for `start-over.bash`
- A private git remote for `archive/` (setup: [docs/archive-and-reload.md](docs/archive-and-reload.md))
- `sudo` and `docker`, only for the root helper below
- Firefly III ≥ 6.7.0 with "batch processing" enabled in its admin configuration
- A Firefly III Personal Access Token in `config/app.env`
- For history imports: the root helper in `deploy/` and a sudo rule for it (see
  `AGENTS.md` → "Root helper")

## Running

Manual:

```bash
./firefly-iii-feeder.bash
```

Cron: TrueNAS cron job every minute as the owning user, with "Hide Standard
Error" off so alerts on stderr are emailed.

Direct (for debugging):

```bash
PYTHONPATH=. python3 -m engine.process_csv <csv_path> <YYYYMMDD-HHMMSS-NNN> <logfile_path>
PYTHONPATH=. python3 -m engine.firefly.import_normalized --dry-run --show 3
# Why a row fails or is skipped: duplicate-index status, every parser step, the exact error
PYTHONPATH=. python3 -m engine.banks.fintro.debug_row <csv> <line-or-Volgnummer>
```

Regression test, before committing a parser change: runs every row of the
originals through the last commit and the working tree, and reports each row
whose result changed (exit 1 if any):

```bash
python -m engine.regression "//<server>/firefly-iii-feeder/archive/originals/fintro/*/*.csv"
```

## Lint

```bash
ruff check .
pre-commit run --all-files   # what every commit runs: ruff, ruff format, shfmt, shellcheck
```

Configured in `ruff.toml` (line-length 120, py310 target,
selects `E,F,W,I,UP,B`).

## Adding a New Bank

1. Drop a `config/<bank>.yaml` defining required columns, regex rules,
   filter values and `duplicate_key`. See `config/fintro.yaml` as reference.
2. Add an `engine/banks/<bank>/` package exposing `normalize_row` in
   `__init__.py` that implements `normalize_row(csv_row) -> dict`.
3. Document the bank in `engine/banks/<bank>/README.md` (same headings as
   Fintro's) and add it to "Supported Banks" (details in `AGENTS.md`).
4. Nothing else to wire up — `autodetect_bank()` matches by CSV header and
   `process_csv.py` imports the bank module dynamically.

## VS Code SFTP Sync

`.vscode/sftp.json` uploads every saved file to the deployment host, so an
edit is live on the next cron minute. Update `host`, `username`,
`privateKeyPath`, and `remotePath` to match your environment. Its `ignore`
list keeps dev files, caches and `config/app.env` off the server, and `data/`,
`archive/`, `data-before-start-over/` and `bank-csv-originals/` out of sync: they
are server state, and the watcher's auto-delete would mirror a local delete there.

## License

MIT — see [LICENSE](LICENSE).
