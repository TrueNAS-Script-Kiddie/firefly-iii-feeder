# Argenta

Two bank modules, one README:

- **Accounts** (current and savings account, xlsx): config [config/argenta.yaml](../../../config/argenta.yaml),
  entry point `normalize_row` in [normalize.py](normalize.py).
- **Mastercard** (monthly PDF statement): config
  [config/argenta_mastercard.yaml](../../../config/argenta_mastercard.yaml), reader
  [statement.py](../argenta_mastercard/statement.py), entry point `normalize_row` in
  [normalize.py](../argenta_mastercard/normalize.py), cross-check
  [archive_check.py](../argenta_mastercard/archive_check.py).

All examples below are made up.

## Export

**Accounts**
- xlsx from Argenta's online banking, **one account per file** (a file with several accounts is
  rejected as a whole). One sheet `Verrichtingen`, row 1 the header, 11 columns.
- Dates are Excel dates and amounts Excel numbers; [engine/core/xlsx_reader.py](../../core/xlsx_reader.py)
  turns them into `YYYY-MM-DD` and the shortest exact number (`-12.5`, `45`).
- Overlapping periods are fine (row state). Originals: `archive/originals/argenta/<IBAN>/`.

**Mastercard**
- The monthly PDF statement, one per month with card activity. The statement date (`Datum`) is the
  last day of the printed period (`Transacties van … tot …`), the 4th of the month; the paying
  account is debited about ten days later.
- Recognized by its text (`Klantenreferentie`, `Transacties van` on page 1), not by its name. A
  PDF from Argenta's generator (Macro 4) that the text reader cannot read fails as unreadable
  instead of passing as "no statement".
- One statement is one export. Originals: `archive/originals/argenta_mastercard/<customer reference>/`,
  named after the printed period (AGENTS.md "Key Directories").

## Columns

**Accounts**

| Argenta column | Internal name | Validation / filter |
|---|---|---|
| Referentie | `external_id` | `^[A-Z][0-9][A-L][0-9]{2}[A-Z0-9]{11}$`; codes the booking date (Two sources) |
| Boekdatum | `primary_transaction_date` | `YYYY-MM-DD` |
| Valutadatum | `interest_date` | `YYYY-MM-DD` |
| Verrichtingsdatum | `transaction_processing_date` | `YYYY-MM-DD` |
| Rekening | `asset_account_iban` | IBAN, spaces allowed |
| Beschrijving | `transaction_type` | not empty; one without a rule fails the row (Parsing) |
| Bedrag | `amount` | `-12.5`; at most 2 decimals |
| Munt | `account_currency_code` | not empty; the module accepts only `EUR` |
| Rekening tegenpartij | `opposing_account_iban` | per Beschrijving: IBAN, dummy IBAN, own IBAN or deposit number |
| Naam tegenpartij | `opposing_account_name` | — |
| Mededeling | `description` | free text; parsed on card rows |

**Mastercard**: the reader turns each statement line into a row with these columns; dates come
out ISO, each `dd/mm` given the latest year that puts it on or before the period end (a statement
can cross New Year).

| Reader column | Internal name | From the statement / validation |
|---|---|---|
| Klantenreferentie | `asset_account_number` | page header; digits |
| Afschriftdatum | `statement_date` | page header `Datum dd/mm/yy[yy]` |
| Volgnummer | `sequence_number` | the row's place on the statement, `01`, `02`, …: repayment lines first, then the card lines in print order |
| Datum transactie | `payment_date` | first date column |
| Datum verrekening | `primary_transaction_date` | second date column; empty only on a repayment line of the oldest layouts, which print one date |
| Omschrijving | `statement_text` | description column, with its continuation lines |
| Bedrag | `amount` | as printed with its sign: `1.234,56-` |
| Munt | `account_currency_code` | column title `BEDRAG (EUR)`; older layouts print no currency and are EUR |
| Origineel bedrag, Originele munt | `foreign_amount`, `foreign_currency_code` | `10,80-USD` in the foreign column |
| Koers | `exchange_rate` | the next line, `1 EUR=1,0800000 USD` |
| Kaartnummer, Kaarthouder | `card_number`, `card_holder` | the section's `Kaartnummer` line; without one, the holder of its `Naam:` line |
| Geldopneming | `is_cash_withdrawal` | `1` on the line a `KOSTEN OP GELDOPNEMING` line follows |

## Unique row

**Accounts**: `duplicate_key` = `Referentie|Bedrag` (`C6I30AB12CD34EF5|-12.5`, Bedrag as the xlsx
reader writes it), one row state per account (`partition_by: asset_account_iban`). Referentie
alone is not unique (Quirks); Bedrag never changes, while Argenta may reword a description. The
key depends on the reader's number form: should that ever change, start over.

**Mastercard**: `duplicate_key` = statement date and sequence number (`2026-09-04|03`), one row
state per card (`partition_by: asset_account_number`, the customer reference): a line has no
reference of its own, and a statement never changes. Two identical lines on one statement (same
day, shop, amount) each get their own key.

## Field mapping

**Accounts**

| Normalized field | Source |
|---|---|
| `external_id` | Referentie |
| `primary_transaction_date` | Boekdatum |
| `interest_date` | Valutadatum |
| `transaction_processing_date` | Verrichtingsdatum |
| `payment_date` | card rows: date and time from the Mededeling |
| `amount` | Bedrag, exact, two decimals (`-12.50`) |
| `account_currency_code` | Munt; anything but `EUR` fails the row |
| `asset_account_iban` | Rekening, without spaces |
| `opposing_account_iban` | Rekening tegenpartij, per rule (Parsing) |
| `opposing_account_number` | the term deposit number, on its start and settlement |
| `opposing_account_name` | Naam tegenpartij; card rows: shop or ATM with place and country from the Mededeling; `Argenta` / `Argenta Mastercard` per rule |
| `is_cash_withdrawal` | `1` for `Opname …` |
| `description` | Mededeling (structured reference → `+++123/4567/89012+++`); empty on card rows and deposit interest, whose Mededeling goes into the fields above and `notes` |
| `notes` | one line each, when present: `Munt: USD` (old card layout, Quirks); transaction type, with the card number on card rows; `Card statement of <date>` (card settlement) or `Term deposit: <number> <code>` (deposit interest) |
| `unmapped_*` | copies of the notes parts, for filtering |

**Mastercard**

| Normalized field | Source |
|---|---|
| `external_id` | empty: a line has no reference |
| `primary_transaction_date` | Datum verrekening; a repayment line with one date: that date |
| `payment_date` | Datum transactie |
| `amount` | Bedrag with its sign (`1.234,56-` → `-1234.56`); zero fails the row |
| `account_currency_code` | Munt; anything but `EUR` fails the row |
| `foreign_amount`, `foreign_currency_code` | Origineel bedrag, signed like `amount`, and Originele munt; checked against the rate (Two sources) |
| `asset_account_number` | Klantenreferentie; `asset_account_iban` stays empty |
| `opposing_account_iban` | transfer to a bank account: the printed pre-IBAN Belgian number as IBAN (`123-1234567-84` → `BE03123123456784`) |
| `opposing_account_name` | purchase, refund, cash withdrawal: Omschrijving; fee: `Argenta`; repayment and reversal: empty (Markers) |
| `is_cash_withdrawal` | `1` from the reader's Geldopneming |
| `is_card_repayment`, `is_payment_reversal` | `1` for a repayment line, a reversal line |
| `notes` | one line each, when present: `Koers: 1,0800000 USD`; transaction type; `Kaart 1234-56XX-XXXX-7890 TOM VAN DER MEULEN` (the number masked as printed; holder only when the statement prints no card number) |
| `unmapped_*` | copies of the notes parts |

The main date of a card line is the settlement date: the repayment line's settlement date is the
day of the debit, the day the paying account books, so both sides of that transfer fall in one
match window (architectural_patterns.md §13).

## Parsing

### Accounts: one rule per `Beschrijving`

Every Beschrijving has a rule in `RULES` ([normalize.py](normalize.py)); an unknown one fails the
row. A rule gives the type line, the required sign and how the counterparty columns are read.

| Beschrijving | Sign | Counterparty | Type line |
|---|---|---|---|
| Inkomende / Uitgaande overschrijving | + / − | IBAN and name as given | `Overschrijving` |
| Inkomende Instantoverschrijving / Uitgaande instantoverschrijving | + / − | IBAN and name as given | `Instantoverschrijving` |
| Betaling Maestro | − | card (below) | `Maestro betaling met debetkaart <card>` |
| Betaling België | − | card | `Betaling met debetkaart <card>` |
| Betaling bancontact in het buitenl. | − | card | `Buitenlandse Bancontact betaling met debetkaart <card>` |
| Opname Maestro | − | card; `is_cash_withdrawal` | `Maestro geldopneming met debetkaart <card>` |
| Opname bancontact in het buitenland | − | card; `is_cash_withdrawal` | `Buitenlandse Bancontact geldopneming met debetkaart <card>` |
| Debet ten voordele van BCC | − | the card's collection IBAN, name `Argenta Mastercard` | `Debet ten voordele van BCC` |
| Storting kredietkaart | + | IBAN and name as given (the card's collection IBAN) | `Storting kredietkaart` |
| Kredietkaart Green-pakket | − | none given → `Argenta` | `Kredietkaart Green-pakket` |
| Taks Portkost Interest | either | the own IBAN, dropped → `Argenta` | `Taks Portkost Interest` |
| Start van een termijndeposito | − | deposit number → `opposing_account_number` | `Start van een termijndeposito` |
| Vereffening van termijnplaatsing | + | deposit number → `opposing_account_number` | `Vereffening van termijnplaatsing` |
| Vereffening van een termijndeposito | + | `Argenta`; deposit number and code to notes | `Vereffening van een termijndeposito` |

`<card>` is the card number from the Mededeling, when the layout has one.

Guards, each failing the row: a card row without the dummy IBAN, a transfer or card settlement
with it; interest without the own IBAN, or with a name; a bank cost with any counterparty; a
deposit row without a deposit number (`[A-Z0-9]+`), or with a name.

### Accounts: the Mededeling of card rows

Phase 1 calls `extract_card_details()` in [extract_card_details.py](extract_card_details.py),
sequential and destructive like Fintro's details. Two layouts:

```
new: [SHOP ]dd-mm-yyyy hh:mm PLACE[ COUNTRY] CARDNR
     ACME MARKT 17-06-2023 11:00 GLASGOW GB 123456*******7890
     → name ACME MARKT GLASGOW GB, payment_date 2023-06-17 11:00, card 123456*******7890
old: CURRENCY NAME   dd-mm-yy hh:mm
     USD SAINT-VAAST        08-05-17 13:01
     → name SAINT-VAAST, payment_date 2017-05-08 13:01, notes Munt: USD
```

New: card number at the end, then date and time; the text before it (shop) and after it (place,
country) together are the name. Old: date and time at the end, then the currency at the start;
the rest is the name. Anything that fits neither raises `ValueError`.

### Mastercard: reading the statement

[engine/core/pdf_text.py](../../core/pdf_text.py) gives the pieces of text with page and x/y; it
refuses anything outside what Argenta's generator writes, so a changed generator fails loudly.
[statement.py](../argenta_mastercard/statement.py) groups pieces on one height (1.5 pt) into lines
and gives each piece the column whose x band it falls in; the bands cover every layout. The
column titles (tiny print) split the page header from the body.

- **Page header**, on every page and identical on all: statement date, page `n/N` (checked
  against the real page count), period, customer reference. Credit limit, `ARGENTA`, the address
  block and notices are dropped.
- **Page 1** (summary): `Vorig saldo van <date>`, repayment lines (`DOMICILIERING VIA UW BANK` or
  `Domiciliëring bij uw bank`, +), one total per card holder, `Nieuw saldo` (with `in uw voordeel`
  on the same or the next line: a balance in the holder's favour, sign +), and
  `Betaling door uw financiële instelling`.
- **Pages 2+**, per holder section closed by `Subtotaal [<holder>]`: `Naam: <holder>`,
  `Kaartnummer[ :] <card> <holder>`, and the card lines (two dates, a description, an amount):

| Line | Kind → row |
|---|---|
| `16/03 17/03 ACME MARKT GLASGOW GBR [10,80-USD] 10,00 -` | purchase (−) or refund (+) |
| `1 EUR=1,0800000 USD` | rate of the line above, which must have a foreign amount |
| `GBR` (text only) | continuation: appended to the description above |
| `KOSTEN OP GELDOPNEMING 5,00 -` | fee of the line above, with its dates; that line becomes a cash withdrawal |
| `OVERDRACHT NAAR BANKREKENING 123-1234567-84 30,00 -` | credit balance paid out to an account |
| `Refund of First presentment - 30,00 +` | reversal of an earlier payment |

Any other line fails the whole statement, so a missed or misread line cannot pass.

**Statement checks**, each failing the whole statement: previous balance + repayments + holder
totals = new balance; per section: its lines add up to its Subtotaal, which equals its total on
page 1 under the same name; every foreign amount has its rate line; `in uw voordeel` matches the
sign of the new balance.

## Two sources

Facts stated twice are compared; a mismatch fails the row (or, on the statement, the statement).

- **Referentie and Boekdatum**: the first five characters code the booking date (`C6I30…` =
  2026-09-30: decade letter A = 2000s, year digit, month letter A = January … L = December, day);
  it must equal Boekdatum.
- **Beschrijving and the sign**: `Inkomende` +, `Uitgaande` −, and the sign of every other rule.
- **Name and Mededeling** (card rows): Naam tegenpartij must be the part before or after the date;
  text before the date that is not the name (an ATM's bank, `A C M E`) belongs to the name.
- **Card settlement**: the day number in `MASTERCARD  <nnn>` must give a statement date at most 31
  days before Boekdatum.
- **Deposit interest**: the Mededeling `<deposit number> - <code>` must name the counterparty's
  deposit number.
- **Foreign amount** (statement): foreign amount ÷ rate, rounded half-up to the cent, must equal
  the EUR amount. The foreign amount's printed sign is always `-`, so the EUR amount gives the sign.
- **Kind and sign** (statement): repayment and reversal +; fee, transfer and cash withdrawal −.

## Dropped on purpose

**Accounts**: `Inkomende`/`Uitgaande` (repeats the sign); the dummy IBAN on card rows; the own IBAN
on interest rows; Naam tegenpartij on card rows (repeats a part of the Mededeling); the currency
`EUR` in an old card Mededeling (repeats the account currency); repeated spaces in names
(`AB         C` → `AB C`).

**Mastercard**: the page header's credit limit, bank name, address block and notices; the column
titles; the balance lines, holder totals and Subtotaal (checks only); `in uw voordeel` (becomes
the sign); `Betaling door uw financiële instelling`; the foreign amount's printed sign; repeated
spaces.

## Markers and conventions

- **Bank as counterparty**: `Argenta` for its own costs, interest and the card's cash withdrawal
  fee; `Argenta Mastercard` for the card settlement.
- **Transaction type wording**: Fintro's where Fintro has the same kind (`Overschrijving`,
  `Instantoverschrijving`, `… met debetkaart`, `… geldopneming …`); the card statement's types
  are `Betaling met kredietkaart`, `Terugbetaling met kredietkaart`, `Geldopneming met
  kredietkaart`, `Kosten op geldopneming`, `Terugboeking betaling met kredietkaart`,
  `Afbetaling kredietkaart: <printed text>` and `Overdracht kredietkaart: <printed text>`.
- **Lines that do not name their counterparty**, left empty for the importer
  (architectural_patterns.md §13): a repayment line (`is_card_repayment`) becomes a transfer from
  the card's `paid_from` account; a reversal (`is_payment_reversal`) gets the counterparty of the
  one earlier withdrawal of the same amount within 180 days, tied by the notes line
  `Oorspronkelijke betaling: <date> (<internal_reference>)`.
- **Cash withdrawals** (`is_cash_withdrawal`) go to Firefly's Cash account, on the account and on
  the card.

## Own accounts

In `archive/accounts.yaml` (template [config/accounts.example.yaml](../../../config/accounts.example.yaml)):

- **Current and savings account**: their IBAN.
- **Mastercard**: `kind: credit_card`; `iban` = the collection account the `Debet ten voordele van
  BCC` rows carry as counterparty (so the monthly debit is a transfer to the card); `account_number`
  = the statement's customer reference (quoted); `paid_from` = the IBAN of the account that pays it
  off (so the repayment line is the other side of that debit).
- **Term deposit**: `kind: savings`, `account_number` = the deposit number, `active: false` once
  it expired: its start and settlement are transfers, its interest income from `Argenta`.
- **Opening balances**: the xlsx has no balance column, and its history is shorter than other
  banks' exports, whose transfers to the account land before its first row. So: today's balance
  (Argenta app) minus the sum of all the account's transactions in Firefly after the load, dated
  before the oldest of them. For the card, whose statements start later than the current account
  pays them: the oldest statement's previous balance (negative when owed) minus the transfers
  into the card before the debit its repayment line matches, dated before the oldest of those.

## Quirks

- **Dummy counterparty IBAN** `BE54 0000 0000 0000` on every card row: its checksum is valid, so
  Firefly would put every shop on one account. It is dropped; the name comes from the Mededeling.
- **Referentie is not unique**: an expired term deposit books principal and interest under one
  Referentie (hence `Referentie|Bedrag`). A transfer between two Argenta accounts carries the same
  Referentie on both sides, on two accounts.
- **Interest names the own account** as counterparty.
- **Card settlement**: `Debet ten voordele van BCC`, named `MASTERCARD  247`: the day of the year
  of the statement date (247 = 4 September 2026), resolved to the year that puts it shortly
  before the booking date. Some old rows leave the name empty: no statement date in the notes.
- **A month without card activity has no statement.** The next statement's previous balance is
  then 0, as the debit paid the last one off, unless that balance was in the holder's favour.
- **Five statement layout generations**, all read by the same bands: header date `dd/mm/yy`
  (older) or `dd/mm/yyyy`; currency in the column title or not; `Kaartnummer` with or without
  ` :`; repayment text `Domiciliëring bij uw bank` or `DOMICILIERING VIA UW BANK`; `Nieuw saldo`
  or `Nieuw Saldo`. Older generations leave out a previous balance of 0, and their repayment lines
  print one date, about ten days before the debit (hence the card's wider transfer window,
  `CARD_REPAYMENT_MATCH_DAYS`). `Vorig saldo 0,00` is printed without a sign.
- An amount and its sign are separate pieces of text; only 0,00 may come without a sign.
- A statement can print no card number: the holder alone names the card.
- The old card Mededeling gives a currency without an amount: a note only, as Firefly takes no
  foreign currency without an amount.

## Checking

On the server (the row state is in `data/`, which is not synced):

```bash
# Mapped columns, filter or validation, row-state status, the rule and every Mededeling step
# (accounts) or the line's kind and the rate check (card), the result or exact failure
PYTHONPATH=. python3 -m engine.debug_row <file> <row> [...]

# Every line of one statement with its kind, then the rows (also on the desktop)
python -m engine.banks.argenta_mastercard.statement <pdf>

# Card statements against the paying account, over the archive
PYTHONPATH=. python3 -m engine.archive_checks --print
```

`<row>` is the "source row" in the normalizer log: the Excel row (`17`) or the statement page and
line (`p3/12`); for the accounts also a Referentie. The traces come from
[debug_trace.py](debug_trace.py) and [../argenta_mastercard/debug_trace.py](../argenta_mastercard/debug_trace.py).

**Archive check** ([archive_check.py](../argenta_mastercard/archive_check.py)), run by
`firefly-iii-feeder.bash` after the importer (architectural_patterns.md §11), per credit card in
the accounts file with archived statements: no single file holds both sides, so it compares the
archived statements with the `Debet ten voordele van BCC` rows of the archived account exports.

- A statement that owes money has one debit of its new balance, within 31 days after it; the
  debit names it by day number, an unnamed one matches by amount.
- Every debit since the first statement has its statement.
- A statement's previous balance is the new balance of the one before, when their periods touch;
  after a gap 0, or the carried balance when it was in the holder's favour.
- Each repayment line is one debit's other side: same amount, within `CARD_REPAYMENT_MATCH_DAYS`
  (15) of its date, on the statement after the debit's own (none after a gap).
- Skipped: debits for statements older than the first archived one, and a statement whose debit
  may still come after the newest row of the paying account's exports.

Before committing a parser change, run the regression test (AGENTS.md "Testing").
