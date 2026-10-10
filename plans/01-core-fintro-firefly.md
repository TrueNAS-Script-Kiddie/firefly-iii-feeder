# Plan 01 — Core, Fintro and Firefly setup

Status: **in progress — steps 0 to 3 done; next: step 4.** To be carried out before
[plan 02 (Argenta)](02-argenta.md).
Nothing here is Argenta-specific, but plan 02 builds on it. Examples are made up (public
repo); real rows stay in the terminal.

Hard requirement for every step: **an idle run stays free** (AGENTS.md "Idle cost"). Everything
below only runs when there is work; after step 1 the idle measurement is repeated, ≤ 2 ms.

## 1. Decisions

1. ✅ **File names in `data/` describe the content** (step 1).
2. ✅ **`Valutadatum` → Firefly `interest_date`**, for all banks. The value date is by definition
   the date from/to which interest runs; Firefly's `book_date` (booking date) is a different
   concept, and the booking date is already in `date`. The internal and normalized field
   `booking_date` is renamed `interest_date` (step 2).
3. ✅ **Foreign currency** in the notes **and** in Firefly's fields `foreign_amount` +
   `foreign_currency_code`, for all banks, with an arithmetic check: a row fails if it does not
   add up to the cent. Fintro: 29 rows, 3 fixed forms, e.g. (made up) `Bedrag: SEK -100,00
   Koers: 10,000000 Wisselkosten: 0,15 EUR` → 100 ÷ 10 + 0.15 = 10.15 = the EUR amount. The rate
   is not always quoted in the same direction (USD multiply, SEK/GBP divide) → the check tries
   both.
4. ✅ **Currencies: no list.** The transactions themselves say which currencies are needed; the
   importer enables a disabled currency (step 3).
5. ✅ **Own accounts via a file** that the feeder applies to Firefly (step 5), using plain words
   for the kind of account.
6. ✅ **That file is `archive/accounts.yaml`** in the archive repo on the server
   ([docs/archive-and-reload.md](../docs/archive-and-reload.md)), edited through the share and committed by the feeder (step 5).
7. ✅ **Whatever must be done by hand is one checklist** in AGENTS.md (step 6).
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
   accounts on both fields (step 3), so money to the savings account and from the current
   account lands on that one account; its balance = what was held at Rabobank in total. One
   correction booking `Rabobank: rest, presumably interest` on the closing date brings it to 0.
   The old current account: its own closed account, with the amount of that one transfer as
   opening balance, ending on 0. The amounts come from the data and go into the accounts file,
   not into the repo.
   Rejected: two accounts with a derived savings→current transfer per payout.

9. ✅ **Documentation: each fact in one place** (step 0). Always valid → AGENTS.md /
   architectural_patterns.md (loaded every session); per bank → `engine/banks/<bank>/README.md`
   (loaded only when someone works on that bank); what other tools may rely on →
   `docs/output-contract.md`. Personal matters live in private repos: the own accounts in the
   archive repo, classification and reporting in the finance repo, which links to this
   contract; the docs mention the latter only as "a private tool".
10. ✅ **Unique row key in Firefly**: `internal_reference` = `<account>|<duplicate key>`
    (step 3). `external_id` is the bank's reference and is not always unique (Argenta) or does
    not exist (card statements); tools that must find a transaction again (classification
    overrides) use the row key.
11. ✅ **Loans as a debt account**: the decision lies with the private finance repo; this repo
    only carries out what is decided there (§5).

## 2. Who loads what into Firefly

Test question: **is it needed to import transactions correctly?** Then the feeder; otherwise the
finance tool (private repo); what is one-off per server or impossible via the API: by hand.

| What | Who | Why |
|---|---|---|
| Own accounts: name, kind/role, IBAN, account number, opening balance + date, active/closed, credit card settings | **feeder** (step 5) | without an account every row fails (`No Firefly asset account with IBAN …`); a full reload becomes repeatable |
| Correction bookings that belong to an account (e.g. the Rabobank rest) | **feeder**, same file | part of the account's history, not of classification |
| Enabling currencies that occur in transactions | **feeder**, at import (step 3) | needed for `foreign_amount` |
| Counterparties (expense/revenue accounts) | Firefly creates them itself on import | merging name variants: finance tool |
| Categories, tags, rules, rule groups, budgets, recurring transactions, piggy banks | finance tool | classification, personal |
| Firefly version, `enable_batch_processing`, Personal Access Token, default currency EUR, root helper + sudo rule, cron job, archive repo (docs/archive-and-reload.md "Setup") | **by hand**, once per server, as a checklist (step 6) | the API stores the batch setting as text (Firefly ignores it); without a token there is no API; the sudo rule, cron and git setup are TrueNAS, not Firefly |
| Language, date format, start page | your preference, not maintained | the feeder does not need them |

## 3. Steps (order = commits)

Every step: `pre-commit`, regression test. What a step may change on Fintro is stated with the
step; everything else must give zero differences.

### Step 0 — Reorganize the documentation ✅ `781da6a`

First, so that every following step documents in the right place at once. Only moving and
splitting; no new content.

```
README.md                          people on GitHub: what it is, getting started, table "Banks" with links
AGENTS.md                          always loaded: hard rules (idle cost, normalization principle,
                                   no real data), running, testing, importer, Firefly checklist,
                                   Start Over, adding a bank; table "Banks" with links
.claude/rules/architectural_patterns.md
                                   always loaded: only the general architecture
docs/output-contract.md            what other tools may rely on
engine/banks/<bank>/README.md      one per bank, fixed layout (below)
engine/banks/<bank>/CLAUDE.md      one line: @README.md
config/accounts.example.yaml       explains the accounts file itself (step 5)
plans/NN-*.md                      temporary (below)
```

1. **Per bank `engine/banks/<bank>/README.md`**, fixed headings: Export (how to get them from
   the bank, format), Columns, Unique row (duplicate key and why), Field mapping (bank →
   internal → normalized), Parsing rules and what is dropped explicitly, Markers and conventions,
   Quirks, Checking (`debug_row`, cross-checks). GitHub shows that README when you open the
   folder. Claude Code loads a `CLAUDE.md` in a subfolder only when it reads files in that
   folder; with `@README.md` in it, the bank documentation is therefore loaded only when the work
   concerns that bank. Other AI tools find them through the "Banks" table in AGENTS.md.
2. **Fintro** gets the first one: from AGENTS.md "Fintro message markers" and the rule about the
   counterparty `Fintro`; from architectural_patterns the Fintro details of §5 (two sources) and
   §6 (parsing the details column). What remains there is the general principle: compare two
   sources, fail on contradiction.
3. **`docs/output-contract.md`**: per normalized field what it means; the words for transaction
   types; markers; format of the notes; which key is unique and stable per bank (`row_key`,
   `external_id`); a short list of changes, so that a tool relying on it knows when to adapt.
   Public, so the private finance repo can link to it.
4. **Plans are temporary**: after execution the lasting facts go to the files above and the plan
   disappears (git keeps it). AGENTS.md says so in one line.

### Step 1 — File names in `data/` ✅ `5a01be5`

Before: `<ts>-<original name>-<stage>`. Two problems (checked in the code and on the server):
- The name does not say which account or period (`CSV_2026-01-31-12.00`, made up); only the
  index backups show the account.
- Not unique: files in the same second get the same `<ts>` (on the server: 5 at 21:20:01). Two
  files with the same name without extension (`202609.pdf`, `202609.csv`, once plan 02 allows
  other formats) would silently overwrite each other's output, log and index backup.

New: the name describes the content, and is unique by how it is made.

```
<run>-<bank>-<account>-<first>_<last>-<stage>.<ext>
```

| Part | What | Made-up example |
|---|---|---|
| `<run>` | start time of the cron run + sequence number of the file in that run (bash counter, builtin) → **unique without waiting** | `20261009-101500-001` |
| `<bank>` | from the config | `fintro` |
| `<account>` | IBAN without spaces (or another account number) | `BE68539007547034` |
| `<first>_<last>` | first and last transaction date in the file | `2026-03-13_2026-10-07` |
| `<stage>` | as now | `processed`, `normalized`, `imported-partial`, … |
| `<ext>` | the original keeps its own extension; all output is `.csv` | `.csv` |

1. **Bash** only passes `<run>` and the path to Python. On a crash (Python stops before
   `finalize`) bash moves the original to `failed/<run>-crashed-<original name>.<ext>` and writes
   to `<run>.log`. "crashed" distinguishes it from `processed/…-processed-failed` (Python
   rejected the file).
2. **Python picks the names**: `build_paths` first makes provisional names (`<run>`), the final
   ones only after bank, account and period are recognized; `finalize` (the single exit path)
   uses those. During the run the log is called `<run>.log` and gets its final name at
   `finalize`. The importer derives its names (`-imported`, `-import.log`, `-import-failed`) from
   the normalized file, as now.
3. **The original name** is on the first line of the log (`Original: gem 260313-261007.csv`),
   and the log names the recognized account (now only `Detected bank: fintro`).
4. **Unknown bank** (rejected before recognition):
   `<run>-unknown-<original name>-processed-failed.<ext>` — then the original name is the only
   handle.
5. **Index backups**: `backups/<run>-<account>-duplicate-index.csv`. The rotation reads the
   account as the last part of the name; IBANs and customer references contain no `-`.
6. Existing files in `data/` keep their old name until the full reload (plan 02).

Fintro regression: zero differences (names are outside the test). Repeat the idle measurement.

Carried out, with three choices that were not stated above:
- The period comes from `date_format` of `primary_transaction_date` in the bank yaml (dates are
  written differently per bank; `engine/core/` knows no bank names).
- A run that starts in a second an earlier run already used (a manual run right after a cron
  run) leaves the work to the next minute: otherwise both would start with `-001`.
- After a failed critical move (exit 92–97) bash names an original `<run>-move-failed-<name>`,
  after a crash `<run>-crashed-<name>`.
Tested in a copy: Fintro export, unknown file, simulated crash, occupied second; regression 0
differences on 14,080 rows; idle on the server 1.9 ms.

### Step 2 — Fintro: value date and foreign currency ✅ `21ce05b`

Separate, so the regression test shows exactly these differences and nothing else:
1. `booking_date` → `interest_date` in `config/fintro.yaml`, `normalize.py`, `debug_row.py`,
   `NORMALIZED_FIELDNAMES` and `build_split`. Expected: every Fintro row shows that rename.
2. New normalized fields `foreign_amount` + `foreign_currency_code` from the existing exchange
   rate text, with the arithmetic check of §1.3. They are added **in addition to, not instead
   of**: the exchange rate text stays complete in `notes` (first line) and in
   `unmapped_exchange_and_transaction_costs`, because that also holds rate and costs, which the
   new fields do not contain. Expected: the 29 rows with a foreign amount get those two fields
   added; `notes` and `unmapped_*` stay unchanged on every row.

Roll out when `data/normalized/` is empty: a file waiting there with the old column name would
lose its value date. **Deploy = save** (AGENTS.md "Deploy = save"): the SFTP watcher puts every
saved file on the server at once. So before the first edit, check on the server that
`data/incoming/` and `data/normalized/` are empty and no `data/*.flag` is pending, and save all
files of this step quickly one after the other. What is already in Firefly keeps `book_date`
until the reload (plan 02).

Carried out, with two points that were not stated above:
- Fintro sometimes prints the rate with the comma one place off (made up: `SEK 100,00 KOERS
  1,000000 WISSELKOSTEN: 0,15 EUR` at -10.15, meant 10,000000). The arithmetic check therefore
  also tries rate ×10 and ÷10. Correcting the original by hand was rejected: a later export of
  the same period has the error again, and the duplicate index (which keeps Details) then
  reports a conflict. In Firefly the rate appears only as text in the notes.
- The rename in `config/fintro.yaml` rewrites every Fintro duplicate index
  (architectural_patterns §7): for rows already in the index the value date is `<not recorded>`
  and is not compared on a new export, until the full reload (plan 02).
Rolled out with `data/incoming/`, `data/normalized/` and `data/failed/` empty and no flag.
Regression: 14,080 rows, each only `booking_date` → `interest_date` (same value) and the two new
fields; 29 rows with a foreign amount, all to the cent; `notes` and `unmapped_*` changed nowhere.

### Step 3 — Importer ✅ `035a425`

1. Own accounts findable by **IBAN and account number** of the Firefly account:
   - own side: `asset_account_iban`, or the new normalized field `asset_account_number`
     (account without IBAN, such as a credit card);
   - counterparty: `opposing_account_iban` or `opposing_account_number`, each searched in both
     fields. This way a Fintro row to the Rabobank current-account IBAN is recognized, even
     though that IBAN is in the account number field (§1.8).
   Old files without `asset_account_number` keep working; empty field = absent field, so no
   difference in the Fintro regression.
2. Safety net: counterparty = the account itself → the row fails with a clear reason (now it
   passes as income/expense with your own IBAN as opposing account; what Firefly does with that
   is untested).
3. `foreign_amount` + `foreign_currency_code` → Firefly's fields of the same name.
4. Currencies: once per import run Firefly's currency list (`GET /v1/currencies`); a disabled
   currency of a row is enabled (`POST /v1/currencies/{code}/enable`, in the API specification
   6.5.5) and logged; a currency Firefly does not know fails the row (creating a currency needs a
   name and symbol, which the feeder does not invent).
5. Row key (§1.10): the normalizer writes a new normalized field `row_key` =
   `<account>|<duplicate key>` (in `process_csv`, not in `normalize_row`, so no difference in the
   regression test); the importer sends it as `internal_reference`. For a transfer between own
   accounts only the first side's row key is in Firefly (as now with `external_id`); the
   contract (step 0) says so.

Checked: **yes, every field sent counts in Firefly's duplicate hash.** `hashArray()` in
`app/Factory/TransactionJournalFactory.php` (6.7.4 in the local KB, unchanged in 6.7.7, the
version on the server) takes the SHA-256 of the whole row as `StoreRequest` reads it: a fixed
set of fields, absent = null, only `import_hash_v2`, `original_source` and `batch_submission`
left out. A row imported before step 2 or 3 therefore gets a different hash now and would be
stored twice if sent again. That only happens if someone puts a file from `data/imported/` back:
a bank CSV dropped again is stopped by the duplicate index. So until the full reload (plan 02)
do not re-import old files; this is stated as a gotcha in AGENTS.md.

Carried out, with choices that were not stated above:
- `asset_account_number` and `row_key` are **at the end** of `NORMALIZED_FIELDNAMES`: Fintro's
  numbered output then does not shift, and readers go by column name.
- `row_key` = `<partition_by value>|<duplicate key>`, so exactly the scope in which the duplicate
  index keeps the key unique; without `partition_by` the bank name.
- An IBAN or account number that belongs to **two** Firefly accounts makes the rows that use it
  fail, instead of picking one.
- Currencies: Firefly does **not** refuse a disabled currency (on saving, `enabled` is checked
  nowhere, and automatic enabling is commented out in the source). Enabling is thus for
  Firefly's own screens, not for the import. It happens per row, just before sending, and
  appears in the import log; `--dry-run` reports "Would enable" and sends nothing.
- `foreign_amount` goes to Firefly without sign, like `amount`.
- AGENTS.md, architectural_patterns §13 and the contract are already updated where they would
  otherwise be wrong; step 6 does the rest.

Tested. Regression: 0 differences on 14,080 rows. Dry run on the server (Firefly 6.7.7), all
files in `data/imported/` (old columns): 14,080 rows, 0 errors, 1,573 transfers found; the
safety net "counterparty = own account" triggers nowhere. Then all Fintro originals normalized in
a throwaway copy and that output through the dry run (without a file on the server): same
counts, 0 errors, `row_key` unique on all 14,080 rows, the 29 rows with a foreign currency carry
both fields and `internal_reference`; USD, GBP and SEK are currently off in Firefly and get
enabled as soon as a row with that currency is really imported (at the latest at the reload).

### Step 4 — Regression test and `debug_row`

1. `engine/regression.py`: identify rows by (account, **duplicate key**) instead of
   `(IBAN, external_id)`, because not every bank has a unique `external_id` (plan 02); glob
   recursively (`**`), so one command covers `archive/originals/`, all banks and
   accounts.
2. Generic `engine/debug_row.py`: columns, duplicate status, result or exact error; a bank may
   add its own step-by-step trace. Fintro's details trace stays.

### Step 5 — Own accounts via a file

Now you create accounts by hand in the Firefly GUI. New: you describe them once in a file, and
the feeder makes Firefly match it.

- Before every import run: create a missing account, update changed fields, never delete
  anything; an own account in Firefly that is not in the file → report.
- Also as soon as the file changes, without a new export: the idle test gets
  `[[ archive/accounts.yaml -nt data/accounts-applied.stamp ]]` (builtin, so an idle run stays
  free); after applying, the stamp is touched. An invalid file → alert, nothing applied.
- Correction bookings go as ordinary transactions with `error_if_duplicate_hash`: applying the
  file again does not book them twice.
- The file is the truth: a GUI change to such an account is overwritten.
- Personal data → **not in this public repo**. The repo gets a template
  `config/accounts.example.yaml` with all explanation and made-up accounts (English, like the
  rest of the repo); your own file is `archive/accounts.yaml`, a fixed path, and starts
  as a copy of it.
- To check during execution: does Firefly refuse an own account with an IBAN that already exists
  as an expense/revenue account (today so for the Argenta and Rabobank IBANs)? Then the order at
  a reload is: wipe Firefly → apply the accounts file → load everything.

Design of the template:

```yaml
# Own accounts in Firefly III, kept in line by firefly-iii-feeder.
#
# What the feeder does with this file, before every import run and when this file changes:
#   - an account listed here but missing in Firefly is created;
#   - a field that differs from Firefly is updated in Firefly;
#   - nothing is ever deleted; an own account in Firefly that is not listed here is reported.
# This file is the truth: a change made in the Firefly GUI to a listed account is overwritten.
# An account is found in Firefly by its iban, else by its account_number.
#
# Fields (one list item per account). Between [brackets]: the field as Firefly's account form
# shows it (English GUI; a translated GUI shows the translation).
#   name                  Required. [Name]
#   kind                  Required. What the account is. [Account role]
#                           checking     current account                     [Default asset account]
#                           savings      savings account or term deposit     [Savings account]
#                           credit_card  credit card with its own statement  [Credit card]
#                                        (needs payment_day)
#                           cash         cash wallet                         [Cash wallet]
#                         A debit card is not an account: it belongs to its checking account.
#   shared                true for an account of more than one person (joint account).
#                         Only with kind checking: the role becomes
#                         [Shared asset account]. Default false.
#   iban                  IBAN, spaces allowed. Bank rows are matched to the account on it. [IBAN]
#   account_number        Second identifier, free text: an account without an IBAN (card
#                         customer reference, term deposit number), or a second IBAN whose rows
#                         belong on this same account. [Account number]
#   payment_day           credit_card only: day of the month the card balance is debited.
#                         [Credit card monthly payment date]; the feeder also sets
#                         [Credit card payment plan] to [Full payment every month].
#   opening_balance       Balance just before the first imported row (negative for a card that
#   opening_balance_date  was owed money). Both or neither. Default 0.
#                         [Opening balance], [Opening balance date]
#   active                false for a closed account. Default true. [Active]
#   corrections           Bookings no export contains (e.g. interest of a closed account without
#                         exports). Each: date, amount (+ in, - out), counterparty (name),
#                         description. Imported like bank rows, so applying the file again does
#                         not book them twice. Not an account field: ordinary transactions.

- name: ACME current account
  kind: checking
  iban: BE68 5390 0754 7034
  opening_balance: 1234.56
  opening_balance_date: 2016-01-01

- name: ACME joint account
  kind: checking
  shared: true
  iban: NL91ABNA0417164300

- name: ACME Mastercard
  kind: credit_card
  payment_day: 14
  iban: DE89 3704 0044 0532 0130 00  # account the card balance is collected on: makes the
                                     # monthly settlement from the current account a transfer
  account_number: "1234567890"       # customer reference on the card statement

- name: Old savings (closed)
  kind: savings
  iban: GB82 WEST 1234 5698 7654 32
  account_number: GB33 BUKB 2020 1555 5555 55  # the bank's current account: its rows land here too
  active: false
  corrections:
    - {date: 2020-12-31, amount: 123.45, counterparty: "Old bank", description: "Rest, presumably interest"}
```

Translation: what you write in the file, what you see in Firefly, what the feeder sends to the
API (the API knows no other values). GUI names from Firefly's own text files
(`resources/lang/en_US/firefly.php`, `form.php`); Firefly's Dutch translation is not in the
source code, so those names were not checked.

| `kind` (+ `shared`) | Firefly GUI, "Account role" | API `account_role` | Extra Firefly then asks for |
|---|---|---|---|
| `checking` | Default asset account | `defaultAsset` | — |
| `checking` + `shared: true` | Shared asset account | `sharedAsset` | — |
| `savings` (also term deposit) | Savings account | `savingAsset` | — |
| `credit_card` | Credit card | `ccAsset` | payment plan "Full payment every month" (`monthlyFull`), monthly payment date (from `payment_day`) |
| `cash` | Cash wallet | `cashWalletAsset` | — |

"Debit" is not a kind of account but a card attached to a current account; in Firefly that card
has no account of its own.

**Where the file lives.** `archive/accounts.yaml`, in the archive repo on the server:
you edit it through the share, the feeder applies it and commits and pushes it with the rest of
the archive. It lives where it is used, so nothing is copied from the desktop.

### Step 6 — Documentation

In the structure of step 0 (AGENTS.md, README, architectural_patterns.md, Fintro README,
output-contract):
- `interest_date`, foreign currency, enabling currencies, own accounts by IBAN or account number,
  `row_key` / `internal_reference` (also in the contract), where steps 2–3 did not already;
- recursive regression command over `archive/originals/`; generic `debug_row`;
- accounts file: template, `archive/accounts.yaml`, applied on change;
- new section **"Firefly setup (by hand, once per server)"**, everything in one place (now
  scattered over the gotchas and "Root helper"), only general steps:
  1. Firefly ≥ 6.7.0.
  2. Admin → Configuration → `enable_batch_processing` on (in the GUI).
  3. Create a Personal Access Token → `FIREFLY_TOKEN` in `config/app.env` (expires after at most
     1 year).
  4. Default currency EUR (also possible via the API, `/v1/currencies/{code}/primary`; one-off,
     so by hand).
  5. Install the root helper + sudo rule for the cron user.
  6. Cron job in TrueNAS (every minute, lock guard, "Hide Standard Error" off).
  7. Archive repo: Forgejo repo, deploy key, `git init` in `archive/` (now in
     docs/archive-and-reload.md "Setup"; move it here and link to it from there).
- repeat the idle measurement and update the number if it changes.

## 4. Testing

1. Regression test after every step: on Fintro only step 2 changes anything (as described
   there).
2. Server: a Fintro CSV via `data/incoming/` → check names, log and output in
   `archive/originals/`, `data/normalized/`, `data/logs/`; importer with `--dry-run`.
3. Accounts file: first with `--dry-run` (shows what it would create/update), then for real.
4. Before every commit: scan the staged diff for IBAN-like strings and real names (public repo).

## 5. Later

- **Loans** as a debt account, if the private finance repo decides so: kind `loan` in the
  accounts file, repayments as a transfer to that account. Principal vs. interest then needs a
  design of its own.
- In the file names a short account name from the accounts file instead of the IBAN.
