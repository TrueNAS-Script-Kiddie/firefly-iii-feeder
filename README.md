# Firefly III Feeder

Automated pipeline for ingesting bank-exported CSVs, validating them,
normalizing transactions into a unified model, deduplicating against a
persistent per-account row state, and importing the result into Firefly III.
Designed for unattended cron execution on TrueNAS or any Linux host.

## Overview

- Detects incoming CSVs in `data/incoming/`
- Auto-detects the bank by matching CSV headers against YAML configs
- Validates, filters and normalizes each row
- Deduplicates via a per-account row state, which also records each row's Firefly transaction
- Imports the normalized rows into Firefly III via its REST API
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
├── start-over.bash                # Wipe Firefly's transactions, reload the archive
├── engine/
│   ├── process_csv.py             # Normalizer entry point
│   ├── regression.py              # Regression test for parser changes
│   ├── core/                      # csv_runtime, csv_validation,
│   │                              # row_state, completion, runtime
│   ├── banks/
│   │   └── fintro/                # Per-bank package: README (export, parsing),
│   │                              # normalize, extract_details, parsers, reconcile, debug_row
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
│   ├── row-state/                 # Per account: known rows, their Firefly id; backups
│   └── logs/ temp/
├── ruff.toml
└── .vscode/sftp.json              # Optional auto-sync to remote host (local, gitignored)
```

## How It Works

Every minute cron starts `firefly-iii-feeder.bash`, which exits at once when there is nothing to
do. Otherwise it waits until each new CSV is fully uploaded, normalizes it (bank, columns, rows
the account's row state already knows are skipped), archives the original, imports the normalized
rows into Firefly III, and commits and pushes the archive. Details:
[.claude/rules/architectural_patterns.md](.claude/rules/architectural_patterns.md).

## Requirements

- Python 3.10+
- `pyyaml` (all other runtime deps are stdlib)
- Bash, `stat`, `tail`, `mv`, `cmp`, `flock`, `git`; `curl` for `start-over.bash`
- A private git remote for `archive/` (setup: [docs/archive-and-reload.md](docs/archive-and-reload.md))
- `sudo` and `docker`, only for the root helper in `deploy/`
- Firefly III ≥ 6.7.0 and a Personal Access Token

One-time setup per server: [AGENTS.md](AGENTS.md), "Setup".

## Running

```bash
./firefly-iii-feeder.bash
```

Cron: TrueNAS cron job every minute as the owning user, with "Hide Standard
Error" off so alerts on stderr are emailed.

Debugging, testing, lint and adding a bank: [AGENTS.md](AGENTS.md) ("Running", "Testing",
"Lint", "Adding a New Bank").

## VS Code SFTP Sync

`.vscode/sftp.json` (local, gitignored: create your own) uploads every saved file to the
deployment host, so an edit is live on the next cron minute. Set `host`, `username`,
`privateKeyPath`, and `remotePath` for your environment. Its `ignore`
list keeps dev files, caches and `config/app.env` off the server, and `data/`,
`archive/`, `data-before-start-over/` and `bank-csv-originals/` out of sync: they
are server state, and the watcher's auto-delete would mirror a local delete there.

## License

MIT — see [LICENSE](LICENSE).
