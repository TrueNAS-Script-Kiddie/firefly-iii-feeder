# Plan 02 — Argenta

Status: **approved; to be carried out after [plan 01](01-core-fintro-firefly.md)**, which brings
the regression test and `debug_row` for every bank, the accounts file and the first start-over.
Examples are made up (public repo); real rows stay in the terminal.

Hard requirement for every step: **an idle run stays free** (AGENTS.md "Idle cost").

## 0. What comes in

| Source | Format | Rows now | Notable |
|---|---|---|---|
| Current account | `.xlsx`, 1 sheet `Verrichtingen`, 11 columns | 250 | 16 kinds of `Beschrijving` |
| Savings account | same `.xlsx` format | 61 | interest with its own IBAN as counterparty |
| Mastercard | monthly **PDF statement**, 110 pieces | ± 430 lines (rough count) | no reference per line, no year per line |

Columns of the xlsx: `Rekening, Boekdatum, Valutadatum, Referentie, Beschrijving, Bedrag, Munt,
Verrichtingsdatum, Rekening tegenpartij, Naam tegenpartij, Mededeling`.

Established in the data:

1. **Dates are Excel numbers** (`45000` = 2023-03-15), amounts are **floats** (`-12.5`, `3.1`;
   made up).
2. **`Referentie` is not unique**: an expired term deposit gives two rows with the same
   reference (principal back + interest). Otherwise unique within an account; a transfer between
   the two Argenta accounts carries the same reference on both sides.
3. `Referentie` encodes the booking date: `C6I30…` = 202**6**, month **I** (A=Jan … L=Dec), day
   **30**. Holds for all 311 rows.
4. Card payments/withdrawals (Maestro, Bancontact abroad) have the **dummy `BE54 0000 0000 0000`**
   as counterparty IBAN — with a valid checksum (calculated), so Firefly would accept it and put
   all shops on one opposing account.
5. Interest (`Taks Portkost Interest`) has the **own account** as counterparty IBAN.
6. Term deposit: `Rekening tegenpartij` holds the **deposit number**, not an IBAN.
7. Card settlement `Debet ten voordele van BCC` (current account → Mastercard): name
   `MASTERCARD  247`; 247 = day number of the statement date (4 September; in a leap year 248).
   A few old rows: name empty.
8. The Mededeling of card payments contains date+time, shop/place, country and card number, in
   two layouts (old: `CURRENCY NAME dd-mm-yy hh:mm`, the currency may be a foreign one; new:
   `[SHOP ]dd-mm-yyyy hh:mm PLACE[ COUNTRY] CARDNR`).
9. PDFs: all from one generator (Macro 4 Columbus), Flate-compressed, WinAnsi fonts, each text
   with absolute x/y. Readable with the stdlib. **5 layout generations.** `pdftotext -layout`
   puts amounts next to the wrong line in older generations → not usable, coordinates are.
10. PDF lines: `dd/mm` transaction + `dd/mm` settlement, description, amount `12,34-`/`+` (old:
    `12,34 -`), foreign currency as `10,80-USD` + next line `1 EUR=1,0800000 USD` (made up). Two
    identical lines on one statement occur (same day, shop, amount).
11. Missing months in the PDF folder coincide with months without a BCC debit on the current
    account (sample): no activity → no statement.
12. Fintro exports contain dozens of rows to/from the Argenta accounts; these are now in Firefly
    as expense/income, not as a transfer between own accounts.
13. Server: Python 3.11, PyYAML, **no** PDF or xlsx library and no `pdftotext`.

## 1. Decisions

1. ✅ **Mastercard as its own Firefly account** (kind `credit_card`), every purchase separately.
2. ✅ **The current account pays off the card: `paid_from` in the accounts file, read by the
   importer.** Example (made up): the statement has a line `DOMICILIERING VIA UW BANK 100,00+`.
   The current account's export has the other side, `Debet ten voordele van BCC -100,00`, with the
   card's IBAN as counterparty, so that row becomes a transfer current account → card. The
   statement line must become the other side of that same transfer. It needs the current
   account as counterparty, and the PDF does not name it.
   - The card account in `archive/accounts.yaml` gets a field `paid_from` (the paying IBAN), also
     in the template `config/accounts.example.yaml`.
   - The normalizer marks the statement line with a new normalized column
     `is_card_repayment` = `1` (like `is_cash_withdrawal`) and leaves its counterparty empty.
   - The importer replaces the empty counterparty by the `paid_from` of the row's own account. No
     `paid_from` → the row fails.

   This way the normalizer, `debug_row` and the regression test never read `archive/`, and a
   reload has the value (docs/archive-and-reload.md, rule 8).
3. ✅ **Card account in the accounts file**: IBAN = the BCC collection number from the
   current-account export (so `Debet ten voordele van BCC` becomes a transfer), account number =
   customer reference of the statement.
4. ✅ **Term deposit** (expired): its own closed account (kind `savings`), deposit number as
   account number → start and repayment are transfers, the interest is income from Argenta.
5. ✅ **Dates**: see §2 "Four names per field". Card PDF: settlement date → main date, because
   the repayment is on the statement with the statement date (the 4th) as transaction date and
   the day of the debit (around the 14th) as settlement date, and the current account books that
   14th; with the 4th the two sides fall outside ±7 days and the transfer appears twice.
6. ✅ **`MASTERCARD 247`** → counterparty name `Argenta Mastercard` (a second Mastercard may
   come some day), note `Card statement of 2026-09-04` (247 = day number of the statement date).
7. ✅ **Start of the card history**: the oldest statements are missing, while the current account
   already pays them off. Opening balance of the card account calculated so that the balance is
   right from the first statement; the purchases before it are missing as separate lines.
   ⏰ **Remind**: the user is still trying to get the older statements from Argenta; ask before
   the reload (§4). If they come, this opening balance and this limitation fall away.
8. ✅ **`Referentie` date check** (point 0.3): the row fails if code and booking date differ.
9. ✅ **Foreign currency** (mechanism: plan 01):
   - Card PDF: `10,80-USD` + `1 EUR=1,0800000 USD` → 10.80 ÷ 1.08 = 10.00 = EUR amount (made
     up), otherwise the line fails.
   - Argenta xlsx, old layout: only the currency, no amount → note only; Firefly does not accept
     a foreign currency without an amount.
10. ✅ **The file name of the original plays no role anywhere**: xlsx and pdf are recognized by
    content (column header, statement text), like CSV now.
11. ✅ **Period in the file names of a statement = the period printed on it**, so you recognize
    the statement by its name. Example (made up): a statement `van 05/11/2018 tot 04/12/2018` is
    named `…-2018-11-05_2018-12-04-…`, whatever dates its lines carry. Mechanism: step 1 (a
    reader may give the file's period) and step 4.
12. ✅ **Numbers from an xlsx are written in their shortest exact form**: `3.1000000000000001`
    (how Excel may store 3.1) → `3.1`, `45.0` → `45`. This is the same value Excel shows, it keeps
    the "max 2 decimals" check of the amount working, and it makes the duplicate key
    (`Referentie|Bedrag`, step 3) the same in every run. Should this rule ever change, the keys
    change with it: start over, like any change to what the feeder derives.

## 2. Data flow and reading

Folders stay what they are; only `incoming/` and the archive (`originals/`,
`unprocessed/`) also receive `.xlsx`/`.pdf`. Names: AGENTS.md "Key Directories",
[docs/archive-and-reload.md](../docs/archive-and-reload.md).

```mermaid
flowchart LR
    IN["incoming/<br/>csv · xlsx · pdf"] -->|normalizer| NO["normalized/<br/>csv"]
    NO -->|importer| IM["imported/<br/>csv"]
    NO -.->|everything to Firefly| FF[(Firefly III)]
    IN -->|original| OR["archive/originals/<br/>own extension"]
    IN -.->|"rows that fail<br/>(normalizer)"| FA["failed-rows/<br/>csv"]
    NO -.->|"rows Firefly refuses"| FA
    IN -.->|"whole file rejected or crashed<br/>(own extension)"| UN["archive/unprocessed/"]
```

No `converted/` intermediate step: the normalizer reads an xlsx/pdf in memory into the same rows
it now reads from a CSV (just as a CSV is not "converted" first now). One step less that can
fail, remain half done or need an alert of its own. To see the table that was read: `debug_row`
on the original file.

Where the reading lives:

| What | Bank-specific? | Where |
|---|---|---|
| xlsx → table (header + rows) | no: every xlsx file is built like this | `engine/core/xlsx_reader.py` |
| PDF → loose pieces of text with page and x/y | no: only the PDF format | `engine/core/pdf_text.py` |
| pieces of text → transaction rows (which line is a purchase, which amount belongs to it, balance check) | **yes**: the layout of Argenta's card statement | `engine/banks/argenta_mastercard/statement.py` |
| row → normalized row | yes | `engine/banks/<bank>/normalize.py`, like Fintro |

The bank's yaml says which reader belongs to its PDFs, so `engine/core/` knows no bank names.

No extra tools on TrueNAS: everything with the Python standard library (checked: Python 3.11).
xlsx = zip with XML files → `zipfile` + `xml.etree`; Argenta PDF = zlib-compressed text with an
x/y position per piece → `zlib` + `re`. Tested on the server itself (read only) on a savings
xlsx and the oldest statement. If Argenta ever changes the PDF generator, the reader refuses the
file with an alert; only then is a container or library worth considering.

Four names per field, from bank to Firefly:

| Argenta xlsx | Internal (yaml) | Normalized CSV | Firefly API |
|---|---|---|---|
| Referentie | `external_id` | `external_id` | `external_id` |
| Boekdatum | `primary_transaction_date` | `primary_transaction_date` | `date` |
| Valutadatum | `interest_date` | `interest_date` | `interest_date` |
| Verrichtingsdatum | `transaction_processing_date` | `transaction_processing_date` | `process_date` |
| — (date+time from Mededeling) | — | `payment_date` | `payment_date` |
| Bedrag | `amount` | `amount` | `amount` without sign; sign → `type` |
| Munt | `account_currency_code` | `account_currency_code` | `currency_code` |
| Rekening | `asset_account_iban` | `asset_account_iban` | `source_id`/`destination_id` (own account looked up by IBAN or account number) |
| — (card PDF: customer reference) | — | `asset_account_number` | idem |
| Rekening tegenpartij | `opposing_account_iban` | `opposing_account_iban`, or `opposing_account_number` (deposit number), or empty (dummy/own IBAN) | `destination_iban`/`source_iban` (`_number`), or transfer |
| Naam tegenpartij | `opposing_account_name` | `opposing_account_name` (+ place from Mededeling) | `destination_name`/`source_name` |
| Beschrijving | `transaction_type` | `unmapped_transaction_type` + line in `notes` | via `notes` |
| Mededeling | `description` | `description` (card rows: parsed, see step 3) | `description` |
| — | — | `opposing_account_bic` (empty: Argenta gives no BIC) | `destination_bic`/`source_bic` |
| — | — | `is_cash_withdrawal` | Firefly's Cash account |
| — (card PDF: repayment line) | — | `is_card_repayment` | transfer from the card's `paid_from` (§1.2) |
| — (card PDF: original amount + currency) | — | `foreign_amount`, `foreign_currency_code` | `foreign_amount`, `foreign_currency_code` |
| — | — | `notes` | `notes` |
| — | — | `unmapped_*` (copy of parts of the notes) | not sent |

"Internal" = the name after reading, which validation, row state and `normalize_row` work
with. "Normalized" = the fixed columns of `NORMALIZED_FIELDNAMES`, the same for every bank. The
Firefly names differ from those; the translation happens only in `build_split()`.

## 3. Steps (order = commits)

Every step: `pre-commit`, regression test (`archive/originals/**/*`, AGENTS.md "Testing"). Fintro:
zero differences. Argenta files: unreadable in both versions until step 3, from then on "new in
this version" in the step that adds them. Each step updates the docs it makes wrong; step 5
lists what is left.

### Step 1 — Accept xlsx and pdf

1. `firefly-iii-feeder.bash`:
   - Every `!(~\$*).csv` glob on `incoming/` (idle check and loop) becomes
     `!(~\$*).@(csv|xlsx|pdf)`, with `shopt -s nocaseglob` so `.PDF` and `.XLSX` count too. Both
     are builtins. Excel leaves `~$<name>.xlsx` lock files like it does for CSV. The `normalized/`
     globs stay `.csv`.
   - Upload check per type, after the existing 30 s ctime wait:
     - CSV: last byte is a newline (as now);
     - xlsx (a zip): the zip end record `PK\x05\x06` in the last 64 KB;
     - PDF: `%%EOF` in the last 1 KB.

     With `tail -c … | grep -q`, only when there is a file. Each keeps the 10-minute fallback.
     The newline test alone would never pass for xlsx or pdf: every such file would wait 10
     minutes unprotected.
   - Repeat the idle measurement, ≤ 2 ms.
   - From this step on, an xlsx or pdf no config recognizes is rejected into
     `archive/unprocessed/` with an alert, instead of being ignored.
2. **`load_csv_rows(path)` → `load_rows(path, bank_configs)`** in `engine/core/csv_runtime.py`.
   It chooses by extension (lower-cased) and returns `(rows, source_lines, period)`:
   - `period`: `(first, last)` as ISO dates when the file states its own period (a statement,
     §1.11), else `None`. `process_csv` uses it for the file names instead of the first and last
     `primary_transaction_date` of the rows;
   - `rows`: dicts with the file's own column names, as now;
   - `source_lines`: a list of texts, one per row, naming where the row is in the file:
     - `.csv`: unchanged reading; source lines `"2"`, `"3"`, … (header = 1, as now);
     - `.xlsx`: new `engine/core/xlsx_reader.py`, stdlib. First sheet, row 1 = header. Source
       line = the Excel row number, so a skipped empty row does not shift the numbers. Cell with
       a date format → `YYYY-MM-DD`; other number → shortest exact form (§1.12); empty cell →
       `""`. Refuses with a clear error: several sheets with data, formulas without a value,
       unknown cell type;
     - `.pdf`: the table comes from a bank-specific reader, named in the yaml (`reader:`). Each
       reader says "not mine" or returns rows with its own column names and a source line
       `p<page>/<line>` per row. After that, `autodetect_bank` works on the header as now.
3. **`validate_and_prepare(rows, bank_cfg, source_lines=None)`** stores `source_lines[i]` as the
   row's `_source_line`; without the argument it counts `"2"`, `"3"`, … as now. `_source_line`
   is a text from now on, never a column of the row, so header detection and the failed-row
   output do not see it. Log lines read as before ("source row 17", now also "source row p3/12").
4. **Callers**:
   - `engine/process_csv.py`: switches to `load_rows`.
   - `engine/debug_row.py`: switches to `load_rows`; its `source_lines` (now counted from 2) come
     from `load_rows`. A `<row>` argument is already compared as text (`17`, later `p3/12`).
   - `engine/regression.py`, `read_file`: calls `load_rows` when the version under test has it,
     else `load_csv_rows`. HEAD keeps the old function until this step is committed.

### Step 2 — Shared helpers

`parse_iban`, `extract_structured_ref` (with `canonicalize_structured_ref`, which it calls),
`normalize_for_comparison`, `apply_replacements` and `parse_comma_decimal_amount` move from
`engine/banks/fintro/parsers.py` to `engine/core/parsers.py`; Fintro imports them from there. No
change in behaviour. Only now, because Argenta is the second user: then it is clear which form
the shared version needs.

### Step 3 — Argenta accounts (`config/argenta.yaml`, `engine/banks/argenta/`)

One config for current and savings account (same columns).

| Column | Internal | Validation |
|---|---|---|
| Referentie | `external_id` | `^[A-Z][0-9][A-L][0-9]{2}[A-Z0-9]{11}$` |
| Boekdatum | `primary_transaction_date` | ISO date; `date_format: "%Y-%m-%d"` (gives the period in the file names) |
| Valutadatum / Verrichtingsdatum | see §2 | ISO date |
| Rekening | `asset_account_iban` | IBAN (spaces allowed) |
| Beschrijving | `transaction_type` | not empty |
| Bedrag | `amount` | `^-?[0-9]+(\.[0-9]+)?$`; exact via `Decimal`, max 2 decimals, → `-2.00` |
| Munt | `account_currency_code` | `EUR`, otherwise the row fails (like Fintro) |
| Rekening tegenpartij, Naam tegenpartij, Mededeling | optional per type | — |

`duplicate_key`: `Referentie|Bedrag` (point 0.2; the amount never changes, Argenta may reword a
description; the text of `Bedrag` as the reader writes it, §1.12). `partition_by:
asset_account_iban`.

**Every `Beschrijving` has a rule of its own; an unknown one makes the row fail.** Types get
Fintro's vocabulary, so the later classification filters the same over both banks.
"Inkomende/Uitgaande" (incoming/outgoing) is dropped explicitly: the direction is in the sign.

| Beschrijving (Argenta) | Counterparty | Description / notes |
|---|---|---|
| Inkomende/Uitgaande (instant)overschrijving | IBAN + name from columns | Mededeling (structured → `+++…+++`); type `Overschrijving` / `Instantoverschrijving` |
| Betaling Maestro, Betaling België, Betaling bancontact in het buitenl., Opname Maestro, Opname bancontact in het buitenland | dummy IBAN **dropped explicitly**; name = shop + place + country | Mededeling parsed (see below); withdrawal → `is_cash_withdrawal=1` |
| Debet ten voordele van BCC | BCC IBAN (→ transfer to card, §1.3) | §1.6 |
| Storting kredietkaart | BCC IBAN (→ transfer from card) | reference from Mededeling |
| Kredietkaart Green-pakket | `Argenta` (bank cost, like `Fintro`) | Mededeling |
| Taks Portkost Interest | **own IBAN dropped explicitly** → `Argenta` | type as note |
| Start van een termijndeposito / Vereffening van termijnplaatsing | deposit number → `opposing_account_number` | §1.4 |
| Vereffening van een termijndeposito (interest) | `Argenta`; deposit number + code (`MMELIQ`, meaning unknown → stays) in notes | — |

Mededeling of card rows (made-up examples), two sources compared as with Fintro:

```
Naam tegenpartij: ACME MARKT
Mededeling:       ACME MARKT 17-06-2023 11:00 GLASGOW GB 123456*******7890
→ name  ACME MARKT GLASGOW GB      payment_date  2023-06-17 11:00
→ notes Betaling met Maestro-debetkaart 123456*******7890

Naam tegenpartij: SAINT-VAAST
Mededeling:       USD SAINT-VAAST        08-05-17 13:01
→ name  SAINT-VAAST                payment_date  2017-05-08 13:01
→ notes Buitenlandse geldopneming met Bancontact; Munt: USD
```

- The name column must occur in the Mededeling, otherwise the row fails (same rule as
  `merge_opposing_account_name`). Text before the date that is not the name (e.g. the bank of the
  ATM, `B B V A`) goes into the name, not dropped.
- Repeated spaces in names (`AB         C`) become one space: formatting, not information.
- A currency `EUR` before the name repeats the account currency → dropped explicitly; any other
  currency stays.

Optional `engine/banks/argenta/debug_trace.py` (AGENTS.md "Key Directories") for the Mededeling
parsing.

### Step 4 — Mastercard statements (`config/argenta_mastercard.yaml`, `engine/banks/argenta_mastercard/`)

1. `engine/core/pdf_text.py` (stdlib): Flate streams → pieces of text with page, x, y, font, text
   (WinAnsi = cp1252, PDF escapes). Refuses everything outside that (encrypted, ToUnicode, object
   streams, other filters) → if Argenta changes generator, the file fails loudly instead of being
   read half.
2. Statement reader: lines on y, columns on x. Header: statement date (older generations
   `dd/mm/yy`, newer `dd/mm/yyyy`), period `from … to …`, customer reference (→
   `asset_account_number`), per card `Kaartnummer`.
   **Year of each `dd/mm`** from the period (a Dec→Jan statement crosses the year boundary), not
   from "nearest date".
3. Kinds of lines:
   - `Vorig saldo` (previous balance): check only, no transaction;
   - repayment (`DOMICILIERING VIA UW BANK` / `Domiciliëring bij uw bank`, `+`):
     `is_card_repayment=1`, counterparty empty (§1.2);
   - purchases and refunds;
   - foreign currency + rate;
   - `Subtotaal` and `Nieuw saldo` (new balance): check only.

   Unknown line → the statement fails.
4. **Checks (Fintro's two-source approach):**
   - **Per statement**, in the reader: previous balance + repayment + all lines = new balance,
     each amount with the sign of the statement (made up: `100,00-` + `100,00+` + `42,50-` =
     `42,50-`). Per card: subtotal = sum of its lines. If it does not add up → the whole
     statement fails, so a wrongly read amount or a missed line cannot get through.
   - **Across sources, automatically after each run**: the statements against the current
     account. A single file never holds both sides, so this check runs over the archive, after
     the normalizer has archived the run's files.
     - What it checks: the new balance of statement N = the BCC debit on the current account
       the month after; every BCC debit since the first statement has a statement; the previous
       balance of statement N+1 = the new balance of N.
     - What it skips: debits before the first statement (§1.7), and a statement whose debit
       lies after the end of the newest current-account export (not known yet).
     - Where: `engine/banks/argenta_mastercard/archive_check.py`, with
       `check_archive(archive_dir) -> list[str]` (one text per difference).
     - Who runs it: a new generic `engine/archive_checks.py`. It finds every
       `engine/banks/*/archive_check.py`, so `engine/core/` still knows no bank names, runs them
       all, and alerts every difference on stderr.
     - When: `firefly-iii-feeder.bash`, after the importer, only when this run processed a bank
       file (of any bank) and `incoming/` holds no export any more. During a reload the
       statements and the current-account exports would otherwise be compared half-loaded. A
       run that leaves a file waiting (still uploading) skips the check, and the run that
       processes that file does it. Off the idle path, like everything after the idle exit.
     - A difference that stays alerts again at every run that processes a file: once per drop,
       as with an account missing from the accounts file (architectural_patterns.md §14).
     - By hand, for `debug_row`-like inspection: `PYTHONPATH=. python3 -m engine.archive_checks --print`
       runs the same checks and prints instead of alerting.

5. Rows: `Klantenreferentie, Afschriftdatum, Volgnummer, Datum transactie, Datum verrekening,
   Omschrijving, Bedrag, Munt, Origineel bedrag, Originele munt, Koers, Kaartnummer`.
   - `duplicate_key` = statement date + sequence number (`2018-12-04/03`, made up): a statement
     never changes, and two identical lines each get their own key that way.
   - `partition_by` = customer reference.
   - The reader returns the printed period `from … to …` as `period` (step 1, §1.11).
6. Importer (`engine/firefly/import_normalized.py`), with the accounts file (architectural_patterns.md §14):
   - `is_card_repayment` at the end of `NORMALIZED_FIELDNAMES`;
   - `paid_from` in the accounts file (template, validation: credit cards only);
   - `build_split`: a row with `is_card_repayment=1` gets the `paid_from` of its own account as
     counterparty, and so becomes a transfer that claims or creates the transfer of the
     current-account row. No `paid_from` → the row fails.
7. All 110 statements must succeed, checked per generation; `engine.archive_checks --print` without differences.

### Step 5 — Documentation

In the structure of plan 01 step 0:

- **`engine/banks/argenta/README.md`** (fixed headings) for the accounts and the card
  statements:
  - export, columns, unique row, field mapping, parsing rules;
  - quirks: dummy IBAN, non-unique reference, own IBAN on interest, BCC settlement, missing
    months = no activity;
  - `paid_from` in the accounts file;
  - checks, incl. the archive check (statements against the current account).

  `argenta/CLAUDE.md` with `@README.md`, `argenta_mastercard/CLAUDE.md` with
  `@../argenta/README.md`.
- **AGENTS.md**:
  - "Idle cost": work is "an export in `data/incoming/`", not "a CSV";
  - "Key Directories": `data/incoming/` takes csv, xlsx, pdf;
  - "Banks": Argenta;
  - "Adding a New Bank": readers for non-CSV (`reader:` in the yaml, `source_lines`, `period`), optional `archive_check.py`;
  - "Running": `engine.archive_checks --print`; "Key Directories": `engine/archive_checks.py`.
- **README.md**: overview (exports instead of CSVs), "Supported Banks", project tree.
- **architectural_patterns.md**:
  - §1: the "Load" step is `load_rows` (CSV, xlsx, bank reader for pdf);
  - §11: the upload check per file type; the archive checks after the importer, and when they run;
  - §13: `is_card_repayment` → `paid_from`.
- **`docs/output-contract.md`**:
  - Keys: Argenta's `external_id` (`Referentie`, not unique) and the card statement (no
    `external_id`);
  - drop "Rows are unique per (`asset_account_iban`, `external_id`)": `row_key` is the unique
    key;
  - the new column `is_card_repayment`;
  - Argenta's transaction types;
  - a line under "Changes".
- **docs/archive-and-reload.md** "Known limitations": drop the bullet that Argenta's exports
  cannot be read yet. `bank-csv-originals/` itself stays (backup, easy to open), and its row in
  AGENTS.md "Key Directories" is not changed.
- This plan disappears; the lasting facts are then in the files above.

## 4. Start over with Argenta (you, on the server)

Needed because the Fintro rows to Argenta (point 0.12) are now expenses/income: without a start-over,
the Argenta import adds a transfer next to them → **counted twice**.

1. ⏰ Ask about §1.7 first (older card statements).
2. Add to `archive/accounts.yaml`, through a dry-run (AGENTS.md "Running"):
   - the Argenta current and savings accounts;
   - the Argenta Mastercard (§1.3), with `paid_from` (§1.2) and its opening balance (§1.7);
   - the term deposit (§1.4).

   The xlsx exports have no balance column: the Argenta opening balance comes from the Argenta
   app or an account statement.
3. `start-over.bash` as the cron user (AGENTS.md "Start Over"): wipes Firefly (also the
   expense/revenue accounts with Argenta IBANs) and reloads the archive; the first run applies
   the accounts file before importing.
4. Right after it, through the share, copy into `data/incoming/` (flat: the files themselves,
   not their folders): the xlsx files of `bank-csv-originals/Argenta/` and the statements in
   `Mastercard/<year>/`. Not before the start-over: cron
   would import them into the old Firefly at once. `bank-csv-originals/` stays as it is.
5. Check:
   - the Firefly balance of every Argenta account = the balance in the Argenta app today;
   - card balance = the last `Nieuw saldo`;
   - every own transfer appears exactly once;
   - nothing in `archive/unprocessed/`.

Takes about an hour (~14,000 Fintro rows in batch + ~750 Argenta rows).

## 5. Testing

1. Regression test after every step: Fintro zero differences; Argenta "new in this version".
2. Argenta: normalize all 311 xlsx rows and all statement lines without error, or the error is
   explained and the parser adjusted.
3. Server: one xlsx and one PDF via `data/incoming/`, check in `data/normalized/`,
   `data/failed-rows/`, `data/logs/`; importer with `--dry-run`.
4. Before every commit: scan the staged diff for IBAN-like strings and real names (public repo).
