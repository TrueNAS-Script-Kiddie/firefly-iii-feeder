# Plan 01 — Core, Fintro and Firefly setup

Status: **in progress — steps 0 to 6 done; step 7: 7.1 and 7.2 done; 7.3 done (Fintro balances
checked roughly: the app has newer transactions); 7.4 done (the combined Rabobank
account ends on the closing transfer to Argenta; account numbers are now read without spaces,
else Firefly's copy differed at every run); next: 7.5, with a new Fintro export.** To be carried out before
[plan 02 (Argenta)](02-argenta.md).
Nothing here is Argenta-specific, but plan 02 builds on it. Examples are made up (public
repo); real rows stay in the terminal.

Hard requirement for every step: **an idle run stays free** (AGENTS.md "Idle cost"). Nothing in
this plan adds a check to the idle path: the accounts file is applied only by runs that import.

## 1. Decisions

1. ✅ **File names in `data/` describe the content** (step 1).
2. ✅ **`Valutadatum` → Firefly `interest_date`**, for all banks (step 2).
3. ✅ **Foreign currency** in the notes **and** in Firefly's `foreign_amount` +
   `foreign_currency_code`, checked to the cent (step 2).
4. ✅ **Currencies: no list.** The importer enables a disabled currency a row uses (step 3).
5. ✅ **Own accounts via a file** that the feeder applies to Firefly (step 5), using plain words
   for the kind of account.
6. ✅ **That file is `archive/accounts.yaml`** in the archive repo on the server
   ([docs/archive-and-reload.md](../docs/archive-and-reload.md)), edited through the share and
   committed by the feeder (step 5).
7. ✅ **Whatever must be done by hand is one checklist**: AGENTS.md "Setup".
8. ✅ **Rabobank.be** (bank code 844, confirmed by a letter from the bank), closed, no more
   exports. The Fintro and Argenta exports show three Rabobank IBANs (numbers, amounts and
   periods only in the chat):

   | Account | Role according to the data |
   |---|---|
   | old current account | one transfer to Fintro, nothing after that |
   | savings account | only receives money, from the Fintro accounts |
   | current account | only gives money: to Fintro, and to Argenta when closing |

   Savings→current went internally at Rabobank and appears nowhere. What went out minus what
   came in is everything that is not visible, presumably interest.
   Chosen: **one** Firefly account for savings + current together: savings IBAN in the IBAN
   field, current-account IBAN in the "account number" field. The importer recognizes own
   accounts on both fields, so money to the savings account and from the current account lands
   on that one account; its balance = what was held at Rabobank in total. One correction booking
   `Rabobank: rest, presumably interest` on the closing date brings it to 0.
   The old current account: its own closed account, with the amount of that one transfer as
   opening balance, ending on 0. The amounts come from the data and go into the accounts file,
   not into the repo.
   Rejected: two accounts with a derived savings→current transfer per payout.
9. ✅ **Documentation: each fact in one place** (step 0).
10. ✅ **Unique row key in Firefly**: `internal_reference` = `row_key` (step 3).
11. ✅ **Loans as a debt account**: the decision lies with the private finance repo; this repo
    only carries out what is decided there (§5).

## 2. Who loads what into Firefly

Test question: **is it needed to import transactions correctly?** Then the feeder; otherwise the
finance tool (private repo); what is one-off per server or impossible via the API: by hand.

| What | Who | Why |
|---|---|---|
| Own accounts: name, kind/role, IBAN, account number, opening balance + date, active/closed, credit card settings | **feeder** (step 5) | without an account every row fails (`No Firefly asset account with IBAN …`); a full reload becomes repeatable |
| Correction bookings that belong to an account (e.g. the Rabobank rest) | **feeder**, same file | part of the account's history, not of classification |
| Enabling currencies that occur in transactions | **feeder**, at import | needed for `foreign_amount` |
| Counterparties (expense/revenue accounts) | Firefly creates them itself on import | merging name variants: finance tool |
| Categories, tags, rules, rule groups, budgets, recurring transactions, piggy banks | finance tool | classification, personal |
| Firefly version, `enable_batch_processing`, Personal Access Token, default currency EUR, root helper + sudo rule, cron job, archive repo | **by hand**, once per server: AGENTS.md "Setup" | the API stores the batch setting as text (Firefly ignores it); without a token there is no API; the sudo rule, cron and git setup are TrueNAS, not Firefly |
| Language, date format, start page | your preference, not maintained | the feeder does not need them |

## 3. Steps (order = commits)

Every step: `pre-commit`, regression test (AGENTS.md "Testing"). On Fintro every step gives zero
differences. Each step updates the docs it makes wrong.

Done; their lasting facts are in AGENTS.md, architectural_patterns.md, the Fintro README,
`docs/output-contract.md`, `docs/archive-and-reload.md`, `config/accounts.example.yaml` and the
docstrings of `engine/regression.py`, `engine/debug_row.py` and `engine/firefly/accounts.py`:

- **Step 0** — documentation reorganized: per-bank README, output contract (`781da6a`).
- **Step 1** — file names in `data/` describe bank, account and period (`5a01be5`).
- **Step 2** — Fintro: `interest_date`, `foreign_amount` / `foreign_currency_code` (`21ce05b`).
- **Step 3** — importer: own accounts by IBAN or account number, foreign amount, currencies,
  `row_key` as `internal_reference` (`035a425`).
- **Step 4** — regression test over `archive/originals/**/*` for every bank and file type, rows
  identified as the row state does, unreadable files listed per version; generic
  `engine/debug_row.py` with an optional per-bank `debug_trace.py` (Fintro's: the details
  steps). Checked: Fintro 0 differences and the same unique rows as before; on the server
  `debug_row` shows what the old Fintro script showed.
- **Step 5** — own accounts from `archive/accounts.yaml` (architectural_patterns.md §14),
  `--accounts-only`, `start-over.bash --wipe-accounts` (`66589a6`). Checked on the server: a
  dry-run of a draft with every own account as it is in Firefly changes only what the draft
  changes on purpose.
- **Step 6** — documentation of step 5 (`4990b62`).

### Step 7 — Start over (on the server)

The first start-over since `9bc800f`. The server still has the old `data/duplicate-index/` and an
empty row state, so until now any export dropped in `data/incoming/` would store its rows a
second time.

1. Write `archive/accounts.yaml`: the draft `data/accounts-draft.yaml` checked in step 5 (every
   own account now in Firefly), plus the two Rabobank accounts of §1.8 and the opening balances.
   Dry-run it (AGENTS.md "Running"); it must create only the Rabobank accounts and change only
   what the draft changes on purpose.
2. `start-over.bash --wipe-accounts` as the cron user (AGENTS.md "Start Over"): the first test
   of rebuilding every account from the file. The first cron run creates them, then loads the
   archive.
3. Check:
   - every Fintro account balance in Firefly = the balance in the bank's app;
   - the old Rabobank current account ends on 0;
   - nothing in `archive/unprocessed/`.
4. Add the Rabobank correction to the file, on the closing date: what left the combined account
   minus what came in, from the archived exports. "Left" includes the closing transfer to the
   Argenta current account, which only the Argenta export holds; plan 02 loads it. So the
   correction is not the balance in Firefly after the start-over. Apply it with
   `--accounts-only`; that account must then end on the amount of the closing transfer, and on
   0 after plan 02.
5. Then the server test of §4.2.

## 4. Testing

1. Regression test after every step: Fintro zero differences.
2. Server, only after step 7: a Fintro export via `data/incoming/` → check names, log and output
   in `archive/originals/`, `data/normalized/`, `data/logs/`; importer with `--dry-run`.
3. Accounts file: always `--dry-run` first (AGENTS.md "Running"), then for real.
4. Before every commit: scan the staged diff for IBAN-like strings and real names (public repo).

## 5. Later

- **Loans** as a debt account, if the private finance repo decides so: kind `loan` in the
  accounts file, repayments as a transfer to that account. Principal vs. interest then needs a
  design of its own.
- In the file names a short account name from the accounts file instead of the IBAN.
