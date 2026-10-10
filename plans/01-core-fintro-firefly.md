# Plan 01 — Core, Fintro and Firefly setup

Status: **in progress — steps 0 to 3 done; next: step 4.** To be carried out before
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
differences. Each step updates the docs it makes wrong; step 6 lists what is left.

Done; their lasting facts are in AGENTS.md, architectural_patterns.md, the Fintro README and
`docs/output-contract.md`:

- **Step 0** — documentation reorganized: per-bank README, output contract (`781da6a`).
- **Step 1** — file names in `data/` describe bank, account and period (`5a01be5`).
- **Step 2** — Fintro: `interest_date`, `foreign_amount` / `foreign_currency_code` (`21ce05b`).
- **Step 3** — importer: own accounts by IBAN or account number, foreign amount, currencies,
  `row_key` as `internal_reference` (`035a425`).

### Step 4 — Regression test and `debug_row`

Neither changes what the cron job does. The regression test runs on the desktop: it needs git, for
two versions of the code. `debug_row` runs on the server: it reads the row state in `data/`.

**1. `engine/regression.py`**

- **Row identity = what the row state uses**: (account, duplicate key), computed per version
  with that version's own code and config:
  - account = the `partition_by` value without spaces, upper case (as `process_csv` does), else
    the bank name;
  - duplicate key = `extract_duplicate_key(row, bank_cfg)`;
  - a row without a key (e.g. it failed validation) is identified by `<file>:<line>`.

  Today it is (IBAN, `external_id`), which breaks for a bank whose `external_id` is not unique
  (plan 02). Printed label: `<file>:<line> <account>|<key>`.
- **Recursive glob**: `glob.glob(pattern, recursive=True)`, files only. Today `**` acts as `*`.
- **A file one version cannot read** (loading or `autodetect_bank` raises) no longer aborts the
  test: that version gets no rows from it, and the report counts the file per version with the
  reason. Rows only the working tree has are a category of their own, "new in this version",
  with their count ok / failed. Plan 02 needs this from its first step.
- Docstring and AGENTS.md "Testing": the command covers every bank and file type at once:

  ```bash
  python -m engine.regression [--base REF] [--show N] "//<server>/firefly-iii-feeder/archive/originals/**/*"
  ```

- Check: Fintro 0 differences, and the same number of unique rows as the old `regression.py`
  reports on the same files (for Fintro both identities are one row per Volgnummer per account).

**2. Generic `engine/debug_row.py`** (replaces `engine/banks/fintro/debug_row.py`)

```bash
PYTHONPATH=. python3 -m engine.debug_row <file> <row> [<row> ...]
```

- Loads the file as `process_csv` does, detects the bank, validates.
- `<row>` = the line as the normalizer log names it ("source row 17"), or a value of
  `external_id` (exact match, also among filtered rows).
- Per row it shows:
  - every mapped column of the bank config (required and optional), not a hard-coded list;
  - when filtered: which column's `filter` / `filter_regex` dropped it, with the value (same
    test as `validate_and_prepare`);
  - when invalid: the validation error;
  - row-state status and Firefly id: `show_duplicate_status` from today's Fintro script, which is
    already generic;
  - the bank's own trace, when the bank has one: optional module
    `engine/banks/<bank>/debug_trace.py` with `show_trace(row: dict) -> None`. Fintro's details
    trace (`extract_details` step by step, the values found) moves into
    `engine/banks/fintro/debug_trace.py`;
  - the `normalize_row` result, or the exact exception.
- `engine/banks/fintro/debug_row.py` is deleted.
- Check, on the server: on a Fintro original in `archive/originals/`, the output for a normal
  row, a filtered (pending) row, a row that fails, and a Volgnummer gives the same information as
  the old script.

### Step 5 — Own accounts via a file

Now you create accounts by hand in the Firefly GUI. New: you describe them once in
`archive/accounts.yaml`, and the feeder makes Firefly match it.

**When it is applied**

- **At the start of every run that imports** (`import_normalized` with files to import), before
  it reads Firefly's asset accounts. A new account only matters together with the export that
  needs it, so cron gets no extra check.
- **By hand**, to apply or try a change without an export, as the cron user:

  ```bash
  PYTHONPATH=. python3 -m engine.firefly.import_normalized --accounts-only [--accounts <path>] [--dry-run]
  ```

  `--accounts-only` applies the file and imports nothing. `--accounts <path>` reads another file
  than `archive/accounts.yaml`. `--dry-run` shows what it would create, update or delete, and
  sends nothing. To change the file safely: write a draft as `data/accounts-draft.yaml` through
  the share (`data/` is not in git), dry-run it with `--accounts data/accounts-draft.yaml`, then
  copy it over `archive/accounts.yaml`. The next run with work commits it with the archive.
- **No file**: nothing is applied, Firefly's accounts are used as they are.

**What applying does**

- Account found in Firefly by its `iban`, else its `account_number`: missing → created; a field
  that differs → updated. Nothing is ever deleted. The file is the truth: a GUI change to a
  listed account is overwritten.
- An own account in Firefly that the file does not list: an alert, in every run that imports,
  until it is in the file or deleted. Every own account belongs in the file, and a run imports
  only when an export was dropped, so this is one mail per drop, not one per minute.
- **Invalid file, or Firefly refuses an account**: handled as "Firefly unreachable" (exit 69,
  architectural_patterns §11): nothing is imported, files stay in `data/normalized/`, one alert
  through the existing `firefly-import-blocked.flag`, retried every minute while files wait; it
  clears once the file is fixed. The alert names the file, the account and the reason.
- **Correction bookings** are ordinary transactions, without a row state. Each gets
  `internal_reference` = `correction|<account>|<sha8>`:
  - `<account>` = the account's IBAN, else its account number, without spaces;
  - `<sha8>` = the first 8 hex digits of the SHA-256 of `<date>|<amount>|<counterparty>|<description>`.

  On every apply the feeder fetches the transactions whose `internal_reference` starts with
  `correction|` (Firefly search operator `internal_reference_starts`). Not in the file → deleted.
  In the file but not in Firefly → created. A changed correction has a new key, so its old version
  is deleted and the new one created. After deleting, `DELETE /api/v1/data/purge`: Firefly's
  duplicate check includes deleted transactions (AGENTS.md "Firefly III Import").
- **Opening balance**: sent as the account fields `opening_balance` + `opening_balance_date`;
  Firefly keeps it as a booking of its own type.
- **Start-over and own accounts.** Checked in Firefly's source:
  - `destroy?objects=transactions` (today's wipe) spares opening balances;
  - `destroy?objects=asset_accounts` deletes every asset account (credit cards included) with
    its opening balance, its piggy banks and its recurring transactions.

  So `start-over.bash` gets two modes:
  - **default**: the own accounts stay, with their piggy banks and recurring transactions. The
    first cron run applies the file, which repairs every listed account: missing ones are
    created, opening balances and other fields set to the file.
  - **`--wipe-accounts`**: also `destroy?objects=asset_accounts`, between the expense/revenue
    accounts and the purge. The first cron run then creates every account again from the file.
    In this mode the script stops before asking when `archive/accounts.yaml` is missing, and its
    banner says that piggy banks and recurring transactions go too.

  After `--wipe-accounts` the accounts have new Firefly ids: the private finance tool must find
  accounts by IBAN or name, never by id.
- **Closed accounts** (`active: false`): checked in Firefly's source. `GET accounts?type=asset`
  lists inactive accounts too, and a transaction may use one as source or destination by id. So
  rows of a closed account still import, also at a reload.
- **Credit card**: Firefly asks a full date for `monthly_payment_date` (only its day is used),
  so the file holds a date too (template below).

**Not in this public repo.** The repo gets a template `config/accounts.example.yaml` with all
explanation and made-up accounts (English, like the rest of the repo). Your own file
`archive/accounts.yaml` starts as a copy of it.

Still to check during execution:

- Does Firefly refuse an own account whose IBAN already exists as an expense/revenue account
  (now so for the Argenta and Rabobank IBANs)? After a start-over it cannot happen: the wipe
  removes expense/revenue accounts before the first run applies the file. Later, when a new
  account is added to the file, it can.
- Does the search operator `internal_reference_starts:` accept the prefix `correction|`? If not,
  choose a prefix it accepts.

Design of the template:

```yaml
# Own accounts in Firefly III, kept in line by firefly-iii-feeder.
#
# Applied at the start of every import run, or by hand:
#   PYTHONPATH=. python3 -m engine.firefly.import_normalized --accounts-only [--accounts <path>] [--dry-run]
#   - an account listed here but missing in Firefly is created;
#   - a field that differs from Firefly is updated in Firefly;
#   - no account is deleted (only start-over.bash --wipe-accounts does); an own account in
#     Firefly that is not listed here gives an alert in every import run.
# This file is the truth: a change made in the Firefly GUI to a listed account is overwritten.
# An account is found in Firefly by its iban, else by its account_number.
# An invalid file blocks the import (one alert); fix the file and the next run goes on.
#
# Fields (one list item per account). Between [brackets]: the field as Firefly's account form
# shows it (English GUI; a translated GUI shows the translation).
#   name                  Required. [Name]
#   kind                  Required. What the account is. [Account role]
#                           checking     current account                     [Default asset account]
#                           savings      savings account or term deposit     [Savings account]
#                           credit_card  credit card with its own statement  [Credit card]
#                                        (needs monthly_payment_date)
#                           cash         cash wallet                         [Cash wallet]
#                         A debit card is not an account: it belongs to its checking account.
#   shared                true for an account of more than one person (joint account).
#                         Only with kind checking: the role becomes
#                         [Shared asset account]. Default false.
#   iban                  IBAN, spaces allowed. Bank rows are matched to the account on it. [IBAN]
#   account_number        Second identifier, free text: an account without an IBAN (card
#                         customer reference, term deposit number), or a second IBAN whose rows
#                         belong on this same account. [Account number]
#   monthly_payment_date  credit_card only: a date whose day is the day of the month the card
#                         balance is debited. [Credit card monthly payment date]; the feeder also
#                         sets [Credit card payment plan] to [Full payment every month].
#   opening_balance       Balance just before the first imported row (negative for a card that
#   opening_balance_date  was owed money). Both or neither. Default 0.
#                         [Opening balance], [Opening balance date]
#   active                false for a closed account. Default true. [Active]
#   corrections           Bookings no export contains (e.g. interest of a closed account without
#                         exports). Each: date, amount (+ in, - out), counterparty (name),
#                         description. Ordinary transactions, not account fields. Applying the
#                         file again books nothing twice; a changed or removed correction
#                         replaces or removes the old one in Firefly.

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
  monthly_payment_date: 2016-01-14
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
| `credit_card` | Credit card | `ccAsset` | payment plan "Full payment every month" (`credit_card_type: monthlyFull`), `monthly_payment_date` |
| `cash` | Cash wallet | `cashWalletAsset` | — |

"Debit" is not a kind of account but a card attached to a current account; in Firefly that card
has no account of its own.

Check: with `--accounts-only --dry-run` on a draft holding the current Fintro accounts as they are
in Firefly, nothing is created or updated. Step 7 does the real test.

### Step 6 — Documentation

What steps 4 and 5 leave, in the structure of step 0:

- **AGENTS.md**:
  - "Running": `engine.debug_row`, `--accounts-only`;
  - "Testing": the recursive regression command;
  - "Key Directories": the Fintro row (no `debug_row`), a row for `engine/debug_row.py`;
  - "Setup": step "Own (asset) accounts" becomes "write `archive/accounts.yaml`" (template
    `config/accounts.example.yaml`).
- **README.md**: the project tree (`debug_row`, `config/accounts.example.yaml`).
- **Fintro README**: "Checking" (`engine.debug_row`, its Fintro trace).
- **architectural_patterns.md** §13: the accounts file is applied before the Firefly state is
  read; an invalid file blocks like an outage. §11: the blocked flag also covers that.
- **docs/archive-and-reload.md**:
  - the archive tree gets `accounts.yaml`;
  - `start-over.bash` section: the diagram, the `--wipe-accounts` mode and its stop without
    `accounts.yaml`;
  - "Not restored": own accounts are repaired or rebuilt from the file now; after
    `--wipe-accounts`, piggy banks and recurring transactions are gone;
  - "Restore on a new server" step 1: no own accounts by hand.
- **AGENTS.md "Start Over"**: own accounts repaired from the file; `--wipe-accounts`.
- **docs/output-contract.md**: corrections and their `internal_reference`.

### Step 7 — Start over (on the server)

The first start-over since `9bc800f`. The server still has the old `data/duplicate-index/` and an
empty row state, so until now any export dropped in `data/incoming/` would store its rows a
second time.

1. Write `archive/accounts.yaml`: every Fintro account, the two Rabobank accounts of §1.8, and
   their opening balances. Dry-run it as in step 5; it must create only the Rabobank accounts
   and change nothing else.
2. `start-over.bash --wipe-accounts` as the cron user (AGENTS.md "Start Over"): the first test
   of rebuilding every account from the file. The first cron run creates them, then loads the
   archive.
3. Check:
   - every Fintro account balance in Firefly = the balance in the bank's app;
   - the old Rabobank current account ends on 0;
   - nothing in `archive/unprocessed/`.
4. Add the Rabobank correction to the file: the combined Rabobank account's balance, sign
   reversed. Apply it with `--accounts-only`; that account must then end on 0.
5. Then the server test of §4.2.

## 4. Testing

1. Regression test after every step: Fintro zero differences.
2. Server, only after step 7: a Fintro export via `data/incoming/` → check names, log and output
   in `archive/originals/`, `data/normalized/`, `data/logs/`; importer with `--dry-run`.
3. Accounts file: always `--dry-run` first (step 5), then for real.
4. Before every commit: scan the staged diff for IBAN-like strings and real names (public repo).

## 5. Later

- **Loans** as a debt account, if the private finance repo decides so: kind `loan` in the
  accounts file, repayments as a transfer to that account. Principal vs. interest then needs a
  design of its own.
- In the file names a short account name from the accounts file instead of the IBAN.
