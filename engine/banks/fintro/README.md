# Fintro

Bank module for Fintro current accounts: config [config/fintro.yaml](../../../config/fintro.yaml),
entry point `normalize_row` in [normalize.py](normalize.py). All examples below are made up.

## Export

- CSV from Fintro online banking, **one account per file** (a file with several accounts is
  rejected as a whole).
- UTF-8 with BOM, `;`-separated, one header row, 13 columns, newest row first.
- Long histories come in several exports; overlapping periods are fine (duplicate index).
- Originals are archived in `archive/originals/fintro/<IBAN>/` (see
  [docs/archive-and-reload.md](../../../docs/archive-and-reload.md)).

## Columns

| Fintro column | Internal name | Validation / filter |
|---|---|---|
| Volgnummer | `external_id` | `YYYY-NNNNN`. A pending row has `YYYY-` without a number: filtered out, it is final in a later export |
| Uitvoeringsdatum | `primary_transaction_date` | `dd/mm/yyyy` |
| Valutadatum | `interest_date` | `dd/mm/yyyy` |
| Bedrag | `amount` | `-1234,56` |
| Valuta rekening | `account_currency_code` | not empty; the module accepts only `EUR` |
| Rekeningnummer | `asset_account_iban` | IBAN |
| Type verrichting | `transaction_type` | — |
| Tegenpartij | `opposing_account_iban` | IBAN, or a foreign account number without IBAN |
| Naam van de tegenpartij | `opposing_account_name` | — |
| Mededeling | `description` | — |
| Details | `details` | free text, parsed (see below) |
| Status | `status` | only `Geaccepteerd`; refused payments (`Geweigerd`) are filtered out |
| Reden van weigering | `reject_reason` (optional) | must be empty on an accepted row, else the row fails |

## Unique row

`duplicate_key` = Volgnummer, one index per account (`partition_by: asset_account_iban`). A
Volgnummer is unique within an account and never changes.

## Field mapping

| Normalized field | Source |
|---|---|
| `external_id` | Volgnummer |
| `primary_transaction_date` | Uitvoeringsdatum |
| `transaction_processing_date` | details `UITGEVOERD OP dd/mm[/yyyy]`; a missing year comes from the date closest to Uitvoeringsdatum |
| `interest_date` | Valutadatum; must equal details `VALUTADATUM` when present |
| `payment_date` | details: date (and time, if given) of a card payment, withdrawal or deposit |
| `amount` | Bedrag, decimal point |
| `account_currency_code` | Valuta rekening; anything but `EUR` fails the row |
| `foreign_amount`, `foreign_currency_code` | details: amount in another currency (`SEK 100,00 KOERS …`), signed like Bedrag; must add up (see Two sources) |
| `asset_account_iban` | Rekeningnummer, without spaces |
| `opposing_account_iban` | Tegenpartij, else the IBAN in details; both present → must match |
| `opposing_account_bic` | details `BIC` |
| `opposing_account_number` | Tegenpartij when it is an account number without IBAN (`123456789012`) |
| `opposing_account_name` | Naam van de tegenpartij + what details add after it (often the address); `Fintro` for the bank's own transactions (see Markers and conventions) |
| `is_cash_withdrawal` | `1` for an outgoing row whose transaction type contains "geldopn" |
| `description` | structured reference (`+++123/4567/89012+++`) from column or details, else Mededeling, else the details message |
| `notes` | one line each, when present: amount in foreign currency with rate and costs; transaction type; bank and technical references |
| `unmapped_*` | copies of the notes parts, for filtering |

## Parsing `Details`

Phase 1 of `normalize_row` (see architectural_patterns.md §6) calls `extract_details()` in
[extract_details.py](extract_details.py). Parsing is sequential and destructive: each matched
segment is cut from the remaining text, so a later pattern cannot match it again.

1. Postfixes anchored with `$`, cut first: `VALUTADATUM`, `BANKREFERENTIE`, `UITGEVOERD OP`, then
   the message (`MEDEDELING : …`, or one of the bank's own texts anchored with `^`, such as
   `TERUGBETALING WOONKREDIET` or `NETTO INTERESTEN`), then `ZONDER MEDEDELING`.
2. The rest, matched by leading pattern anchored with `^`: VERBETERING, STORTING, DOORLOPENDE
   OPDRACHT, DOMICILIERING, BUITENLANDSE OVERSCHRIJVING, OVERSCHRIJVING, BETALING, ANNULERING
   BETALING, MOBIELE BETALING, GELDOPNEMING.
3. Old-card fallback, only for rows before 2018-09-01: whatever is left is the counterparty, after
   an optional card number, even when another pattern already matched. Not guarded further,
   because that history is complete: every such row is in the originals and covered by the
   regression test.

Anything left at the end raises `ValueError`: the row fails, and the fix goes into the parser.

## Two sources

Many facts appear both in a dedicated column and inside `Details`. Phase 2 compares them and
merges or raises on a mismatch; neither source is trusted blindly.

- **Column wins** for amount, IBAN, dates and the free-text message (details only validate).
- **Details win** for structured references, BIC, and facts missing from the columns.
- **A structured message** (`+++xxx/xxxx/xxxxx+++`) takes priority over free text when either
  source has one (`extract_structured_ref` in [parsers.py](parsers.py)).
- **Counterparty name** (`merge_opposing_account_name` in [reconcile.py](reconcile.py)): the column
  name plus whatever details add after it. A leading filler `VAN` is dropped only when the column
  name proves it is not part of the name (`VAN DER MEULEN TOM`). Text before the column name, or a
  different name, fails the row.
- **Foreign amount** (`reconcile_foreign_amount` in [reconcile.py](reconcile.py)): foreign amount
  converted at the rate, plus all costs in EUR, must equal Bedrag to the cent, else the row fails.
  Fintro writes the rate either way round (foreign per EUR or EUR per foreign, not fixed per
  currency), so both are tried. The text itself stays in `notes` unchanged.
- **Transaction type** (`reconcile_transaction_types` in [reconcile.py](reconcile.py)): column and
  details type are reduced to one; when both remain after the known rules, the row fails.

## Dropped on purpose

Only text that adds nothing, each through a named pattern: labels (`MEDEDELING :`,
`VALUTADATUM :`, `BANKREFERENTIE :`), fillers (`VAN`, `UW`, `OP REKENING`, `VAN REKENING`, `NAAR`,
`IN EURO`, `OM`), `DETAILS ZIE BIJLAGE` and the loyalty-bonus sentence, ` in euro` after a column
type, and an amount in details that exactly repeats the row amount (`EUR 1.234,56` for
`-1234,56`). Card numbers are reformatted (`1234 56XX XXXX X123 4` → `1234 56XX XXXX 1234`), not dropped.

## Markers and conventions

- **Message markers.** Incoming payments from employers, health funds and unions start their free
  text with `/A/`, `/B/` or `/C/`. The marker stays in `description` unchanged. By payer (inferred
  from the data, no official definition found): `/A/` wages (employers), `/B/` replacement-income
  benefits (health fund, union, unemployment fund), `/C/` reimbursed care (health fund, lines like
  `PREST HUISARTS`). Meant for filtering/tagging later.
- **Bank as counterparty.** Fintro leaves the counterparty empty for its own transactions (loans,
  fees, interest, bonus). For the types in `BANK_COUNTERPARTY_TRANSACTION_TYPES`
  ([normalize.py](normalize.py)) the counterparty becomes `Fintro`.
- **Transaction type wording**: the `REPLACE_IN_*` tables in [normalize.py](normalize.py) map
  Fintro's texts to one consistent wording (`Betaling met debetkaart`, `Instantoverschrijving`, …).

## Quirks

- `Netto interesten`: the description combines the details text with the column message.
- The yearly loyalty bonus comes with type `Kosten rekeningbeheer`; its type becomes
  `Opbrengsten in verband met de rekening`.
- Amounts inside details may carry thousands dots (`1.234,56`).
- **Fintro misprints the exchange rate in its own exports**: the decimal point one place off, e.g.
  `SEK 100,00 KOERS 1,000000 WISSELKOSTEN: 0,15 EUR` for -10,15 where `KOERS 10,000000` is meant
  (100 / 10 + 0,15 = 10,15). Bedrag and the foreign amount are right; only the printed rate is
  wrong. So the cent check also tries the rate x10 and /10. The originals are not corrected by
  hand: a later export of the same period carries the misprint again, and the duplicate index
  (which keeps Details) would then report the row as a conflict. `notes` keeps the rate as printed.
- Dates are `dd/mm/yyyy`; Firefly would read them as US month/day, so the module writes ISO.

## Checking

On the server (the duplicate index is in `data/`, which is not synced):

```bash
# Duplicate-index status, every details step, the values found, the result or exact failure
PYTHONPATH=. python3 -m engine.banks.fintro.debug_row <csv> <line-or-Volgnummer> [...]
```

`<line>` is the "source row" in the normalizer log, or a Volgnummer (`2019-00135`; also finds
filtered rows such as pending ones). Before committing a parser change, run the regression test
(AGENTS.md "Lint").
