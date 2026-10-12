# Plan 02 — Argenta

Status: **built, documented and loaded; the rebuild proof is still to do.** How the feeder reads
Argenta: [engine/banks/argenta/README.md](../engine/banks/argenta/README.md).

## Rebuild proof (you, on the server)

`start-over.bash` as the cron user (AGENTS.md "Start Over"), as proof that the archive alone
rebuilds Fintro and Argenta together: read the balances and counts first, compare them after the
reload (about an hour). It also gives the cash withdrawals already in Firefly their new
destination, Firefly's Cash account (docs/output-contract.md, "Changes", 2026-10-11).

Then this plan is done and goes.
