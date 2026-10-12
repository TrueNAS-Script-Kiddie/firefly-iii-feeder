# Output contract

What other tools (a classifier, reports, Firefly rules) may rely on. Anything not listed here can
change without notice. A change to something listed here is recorded under "Changes", so a tool
that depends on it knows when to adapt.

## Normalized CSV

One file per bank export in `data/normalized/` (then `data/imported/`): UTF-8, `;`-separated, one
header row, the columns of `NORMALIZED_FIELDNAMES` ([engine/process_csv.py](../engine/process_csv.py))
in that order. Every bank fills the same columns; empty means "not given by the bank".

| Column | Meaning | Format |
|---|---|---|
| `external_id` | the bank's own reference for the row | bank-specific (see "Keys"), or empty when the bank gives none |
| `primary_transaction_date` | the row's main date; which bank column it comes from is in the bank README | `YYYY-MM-DD` |
| `transaction_processing_date` | the date the order was executed, when the bank gives it separately | `YYYY-MM-DD` |
| `interest_date` | the value date (Valutadatum): from/until when interest runs | `YYYY-MM-DD` |
| `payment_date` | when the card was used, cash taken or deposited | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `amount` | signed amount in the account currency: negative = money out | `-1234.56` |
| `account_currency_code` | currency of the account | `EUR` (other currencies fail the row) |
| `foreign_amount` | amount in another currency, when paid in one; checked against `amount` | `-1234.56`, signed like `amount`, or empty |
| `foreign_currency_code` | currency of `foreign_amount` | `USD`, or empty |
| `asset_account_iban` | the own account the row belongs to | IBAN without spaces, or empty for an account without one (then `asset_account_number`) |
| `opposing_account_iban` | counterparty IBAN | IBAN without spaces, or empty |
| `opposing_account_bic` | counterparty BIC | or empty |
| `opposing_account_number` | counterparty account number that is not an IBAN (e.g. a term deposit number) | free text, or empty |
| `opposing_account_name` | counterparty name, with what the bank adds (address, place) | free text; the bank's name for its own fees and interest |
| `is_cash_withdrawal` | cash taken out | `1`, or empty |
| `description` | the message: a structured reference, else the free-text message | `+++123/4567/89012+++` or free text; may be empty |
| `notes` | everything else that says something about the row, one fact per line (below) | text with `\n` between lines |
| `unmapped_exchange_and_transaction_costs` | the foreign-amount/costs line of `notes` | text, or empty |
| `unmapped_transaction_type` | the transaction-type line of `notes` | text |
| `unmapped_reference_parts` | the references line of `notes` | text, or empty |
| `asset_account_number` | the own account, when it has no IBAN (e.g. a credit card) | free text, or empty |
| `row_key` | unique, stable key of the row (see "Keys") | `<account>\|<duplicate key>` |
| `is_card_repayment` | a card statement's repayment line: money from the account that pays the card off, which the statement does not name; the counterparty is left empty: the importer books a transfer from the card's `paid_from` account in the accounts file (none → the row fails) | `1`, or empty |
| `is_payment_reversal` | set only by the card statement: the reversal of an earlier card payment, which the statement does not name; the counterparty is left empty: the importer takes as source the destination of the one withdrawal from the same own account with exactly the same amount and currency in the 180 days before the row's date that no other reversal took (none or several → the row fails) | `1`, or empty |

**`notes`**, lines in this order, each only when present:
1. foreign amount, rate and costs, as the bank writes them after a label
   (`Bedrag: USD -12,34 Koers: 1,080000 Wisselkosten: 0,15 EUR`, `Behandelingskosten: 0,25 EUR`);
2. transaction type, in the bank's consistent wording (below, and each bank README, "Markers and
   conventions");
3. references (`Bankreferentie: …`, mandate and payer references).

**Markers** that may be relied on are listed in each bank README, with their meaning (Fintro:
`/A/`, `/B/`, `/C/` at the start of `description`).

**Transaction types** (the type line of `notes`, `unmapped_transaction_type`):

| Bank | Types |
|---|---|
| Fintro | its README, "Markers and conventions" |
| Argenta accounts | `Overschrijving`, `Instantoverschrijving`, `Maestro betaling met debetkaart`, `Betaling met debetkaart`, `Buitenlandse Bancontact betaling met debetkaart`, `Maestro geldopneming met debetkaart`, `Buitenlandse Bancontact geldopneming met debetkaart` (card types followed by the card number when the bank gives it), `Debet ten voordele van BCC`, `Storting kredietkaart`, `Kredietkaart Green-pakket`, `Taks Portkost Interest`, `Start van een termijndeposito`, `Vereffening van termijnplaatsing`, `Vereffening van een termijndeposito` |
| Argenta Mastercard | `Betaling met kredietkaart`, `Terugbetaling met kredietkaart`, `Geldopneming met kredietkaart`, `Kosten op geldopneming`, `Terugboeking betaling met kredietkaart`, `Afbetaling kredietkaart: <printed text>`, `Overdracht kredietkaart: <printed text>` |

## Keys

| Bank | `external_id` | Unique within an account? | Stable? | Duplicate key |
|---|---|---|---|---|
| Fintro | Volgnummer, `YYYY-NNNNN` | yes | yes | Volgnummer |
| Argenta accounts | Referentie | no: an expired term deposit books principal and interest under one | yes | `Referentie\|Bedrag` |
| Argenta Mastercard | empty: a statement line has no reference | — | — | statement date and sequence number on the statement, `2026-09-04\|03` |

`row_key` is the unique and stable key for every bank: the account (the row state's
`partition_by` value: the IBAN without spaces, or a card's customer reference) and the row's
duplicate key, joined by `|` (Fintro: `BE68539007547034|2026-00123`). A tool that must find a
transaction again uses it, not `external_id`.

## In Firefly

The importer turns each row into one Firefly transaction (`build_split` in
[engine/firefly/import_normalized.py](../engine/firefly/import_normalized.py)):

| Firefly field | From |
|---|---|
| `date` | `primary_transaction_date` |
| `interest_date` | `interest_date` |
| `process_date` | `transaction_processing_date` |
| `payment_date` | `payment_date` |
| `amount`, type | `amount` without sign; negative → withdrawal, positive → deposit; counterparty is an own account → transfer |
| `currency_code` | `account_currency_code` |
| `foreign_amount`, `foreign_currency_code` | `foreign_amount` without sign, `foreign_currency_code` |
| `description` | `description`, else the counterparty name, else the first line of `notes` |
| `notes` | `notes`; a reversal adds `Oorspronkelijke betaling: <date> (<internal_reference>)`, naming the payment it reverses |
| `external_id` | `external_id` |
| `internal_reference` | `row_key` |
| source / destination | own account by IBAN or account number (`asset_account_iban`, else `asset_account_number`, each matched against both fields of the Firefly account); counterparty by name, IBAN, account number and BIC; no counterparty → `(onbekend)`; a cash withdrawal always → Firefly's Cash account, with the counterparty name (the ATM) in front of `description`; a card repayment → transfer from the card's `paid_from` account; a reversal → from the counterparty of the payment it reverses |

A transfer between own accounts is stored once: it carries the fields of whichever side was
imported first; the other side's `external_id`, `row_key` and `notes` stay in its `data/imported/`
file only.

**Corrections** from the accounts file (bookings no export contains; architectural_patterns.md §14)
are transactions without a bank row: `internal_reference` = `correction|<account>|<sha8>`, notes
`Correction from the accounts file`, no `external_id`. A changed correction is a new transaction
with a new key; the old one is deleted.

**Own accounts** are set from the accounts file and may get new Firefly ids at a start-over: find
them by IBAN, account number or name, never by id.

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | First version, describing the output as it is. |
| 2026-10-09 | `booking_date` renamed `interest_date`, sent to Firefly's `interest_date` instead of `book_date`. New columns `foreign_amount`, `foreign_currency_code` (not yet sent to Firefly). |
| 2026-10-09 | New columns `asset_account_number` and `row_key`, added at the end. Sent to Firefly: `foreign_amount` / `foreign_currency_code`, and `row_key` as `internal_reference`. Own accounts are found by IBAN or account number. |
| 2026-10-11 | Own accounts and correction bookings come from the accounts file: corrections carry `internal_reference` `correction\|…`; own account ids may change at a start-over. |
| 2026-10-11 | A cash withdrawal (`is_cash_withdrawal`) always goes to Firefly's Cash account, also when the bank names the ATM; that name goes in front of the Firefly `description`, not to an expense account. Applies to rows already in Firefly after a start-over. |
| 2026-10-12 | New column `is_card_repayment`, added at the end: a transfer from the card's `paid_from` account. |
| 2026-10-12 | New column `is_payment_reversal`, added at the end: a deposit from the counterparty of the payment it reverses, with a notes line naming that payment. |
| 2026-10-12 | Argenta accounts and Argenta Mastercard statements. `external_id` may be empty (card lines) or not unique (Argenta's Referentie): `row_key` is the unique key. `asset_account_iban` is empty on card rows, which name their account by `asset_account_number`. |
