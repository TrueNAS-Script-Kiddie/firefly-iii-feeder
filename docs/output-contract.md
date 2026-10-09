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
| `external_id` | the bank's own reference for the row | bank-specific (see "Keys") |
| `primary_transaction_date` | the row's main date; which bank column it comes from is in the bank README | `YYYY-MM-DD` |
| `transaction_processing_date` | the date the order was executed, when the bank gives it separately | `YYYY-MM-DD` |
| `interest_date` | the value date (Valutadatum): from/until when interest runs | `YYYY-MM-DD` |
| `payment_date` | when the card was used, cash taken or deposited | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `amount` | signed amount in the account currency: negative = money out | `-1234.56` |
| `account_currency_code` | currency of the account | `EUR` (other currencies fail the row) |
| `foreign_amount` | amount in another currency, when paid in one; checked against `amount` | `-1234.56`, signed like `amount`, or empty |
| `foreign_currency_code` | currency of `foreign_amount` | `USD`, or empty |
| `asset_account_iban` | the own account the row belongs to | IBAN without spaces |
| `opposing_account_iban` | counterparty IBAN | IBAN without spaces, or empty |
| `opposing_account_bic` | counterparty BIC | or empty |
| `opposing_account_number` | counterparty account number that is not an IBAN | free text, or empty |
| `opposing_account_name` | counterparty name, with what the bank adds (address, place) | free text; the bank's name for its own fees and interest |
| `is_cash_withdrawal` | cash taken out | `1`, or empty |
| `description` | the message: a structured reference, else the free-text message | `+++123/4567/89012+++` or free text; may be empty |
| `notes` | everything else that says something about the row, one fact per line (below) | text with `\n` between lines |
| `unmapped_exchange_and_transaction_costs` | the foreign-amount/costs line of `notes` | text, or empty |
| `unmapped_transaction_type` | the transaction-type line of `notes` | text |
| `unmapped_reference_parts` | the references line of `notes` | text, or empty |

**`notes`**, lines in this order, each only when present:
1. foreign amount, rate and costs, as the bank writes them after a label
   (`Bedrag: USD -12,34 Koers: 1,080000 Wisselkosten: 0,15 EUR`, `Behandelingskosten: 0,25 EUR`);
2. transaction type, in the bank's consistent wording (each bank README, "Markers and conventions");
3. references (`Bankreferentie: …`, mandate and payer references).

**Markers** that may be relied on are listed in each bank README, with their meaning (Fintro:
`/A/`, `/B/`, `/C/` at the start of `description`).

## Keys

| Bank | `external_id` | Unique within an account? | Stable? |
|---|---|---|---|
| Fintro | Volgnummer, `YYYY-NNNNN` | yes | yes |

Rows are unique per (`asset_account_iban`, `external_id`) for the banks above.

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
| `description` | `description`, else the counterparty name, else the first line of `notes` |
| `notes` | `notes` |
| `external_id` | `external_id` |
| source / destination | own account by IBAN; counterparty by name, IBAN, account number and BIC; no counterparty → `(onbekend)`, or Firefly's Cash account for a cash withdrawal |

A transfer between own accounts is stored once: it carries the fields of whichever side was
imported first; the other side's `external_id` and `notes` stay in its `data/imported/` file only.

## Changes

| Date | Change |
|---|---|
| 2026-10-09 | First version, describing the output as it is. |
| 2026-10-09 | `booking_date` renamed `interest_date`, sent to Firefly's `interest_date` instead of `book_date`. New columns `foreign_amount`, `foreign_currency_code` (not yet sent to Firefly). |
