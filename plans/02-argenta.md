# Plan 02 — Argenta

Status: **built and documented; the load on the server is still to do.** How the feeder reads
Argenta: [engine/banks/argenta/README.md](../engine/banks/argenta/README.md).
Examples are made up (public repo); real values stay in the terminal.

## Load Argenta (you, on the server)

No start-over is needed: the Argenta current and savings accounts are already in the accounts
file, so the Fintro rows to them are transfers with one side, which the Argenta rows claim. The
card and the term deposit are created by the first run that imports; their collection IBAN and
deposit number are on no expense or revenue account (only Argenta's own exports carry them), so
Firefly accepts them as own accounts.

1. ⏰ Ask first whether the older card statements came from Argenta. If they did, add them to the
   files of step 3; the card's opening balance (step 2) then follows from the oldest of them.
2. Through the share, copy `archive/accounts.yaml` to `data/accounts-draft.yaml` and add:
   - the opening balance of the Argenta current and savings accounts (already listed): the
     balance just before the oldest row of their exports. The xlsx has no balance column: take it
     from the Argenta app or an account statement.
   - the card:

     ```yaml
     - name: Argenta Mastercard
       kind: credit_card
       monthly_payment_date: 2016-01-14   # day of the month the card balance is debited
       iban: DE89 3704 0044 0532 0130 00  # 'Rekening tegenpartij' of the 'Debet ten voordele van BCC' rows
       account_number: "1234567890"       # 'Klantenreferentie' on the statement
       paid_from: BE68 5390 0754 7034     # the Argenta current account
       opening_balance: -123.45
       opening_balance_date: 2016-01-01
     ```

     The card's history starts at the oldest statement, while the current account already paid
     older ones: each of those debits becomes a transfer into the card without the purchases it
     paid. So the opening balance = the oldest statement's `Vorig saldo` (negative when owed)
     minus what the transfers between card and current account add to the card before the debit
     that statement's repayment line matches (debits +, `Storting kredietkaart` −); dated before
     the oldest of those transfers.
   - the term deposit:

     ```yaml
     - name: Argenta term deposit
       kind: savings
       account_number: "ABC123456"   # 'Rekening tegenpartij' of its start row
       active: false
     ```

   Dry-run it as the cron user, then copy it over `archive/accounts.yaml`:

   ```bash
   PYTHONPATH=. python3 -m engine.firefly.import_normalized --accounts-only --accounts data/accounts-draft.yaml --dry-run
   ```

3. Read the balances and transaction counts in Firefly, to compare after. Through the share, copy
   into `data/incoming/` (flat: the files themselves, not their folders) the xlsx files of
   `bank-csv-originals/Argenta/` and the statements in `bank-csv-originals/Argenta/Mastercard/<year>/`.
   `bank-csv-originals/` stays as it is. A few minutes (~800 rows, batch mode). Card lines in a
   currency Firefly lacks create it: the token must be Firefly's owner's (AGENTS.md "Setup").
4. Check:
   - nothing in `archive/unprocessed/`, no Argenta file in `data/failed-rows/`, no alert mail;
   - `PYTHONPATH=. python3 -m engine.archive_checks --print`: no differences;
   - the Firefly balance of every Argenta account = the balance in the Argenta app today; the
     card's = the last statement's `Nieuw saldo`;
   - every own transfer appears exactly once (Fintro ↔ Argenta, current ↔ savings, current ↔ card,
     current ↔ term deposit);
   - the combined Rabobank account ends on 0 (its closing transfer to Argenta is in the Argenta
     export; the correction in the accounts file is only the rest).
5. `start-over.bash` as the cron user (AGENTS.md "Start Over"), as proof that the archive alone
   rebuilds Fintro and Argenta together: read the balances and counts first, compare them after
   the reload (about an hour). It also gives the cash withdrawals already in Firefly their new
   destination, Firefly's Cash account (docs/output-contract.md, "Changes", 2026-10-11).
