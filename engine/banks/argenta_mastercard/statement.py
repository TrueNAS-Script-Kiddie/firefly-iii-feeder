"""
Argenta Mastercard statement (PDF) -> one row per statement line, for engine.core.csv_runtime.load_rows.

Every page repeats a header above the column titles: statement date, page n/N, the period
('Transacties van … tot …') and the customer reference. Page 1 is the summary, pages 2+ hold the
card lines. Lines are the pieces of text on one height (LINE_TOLERANCE); a piece's column is the
band its x falls in (BANDS), which covers every layout generation.

Line kinds below the column titles (examples made up):
  page 1   'Vorig saldo van 04/03/2018  100,00 -'           previous balance, check only (left out
                                                            when 0 in older generations)
           '05/03 [14/03]  DOMICILIERING VIA UW BANK  100,00 +'  repayment -> row
           'DHR. TOM VAN DER MEULEN  42,50 -'               a card holder's total, check only
           'Nieuw saldo [in uw voordeel]  42,50 -'          new balance, check only; 'in uw voordeel'
                                                            (also on a line of its own) = sign +
           'Betaling door uw financiële instelling'         how the balance is paid, dropped
  pages 2+ 'Naam: DHR. TOM VAN DER MEULEN'                  card holder section
           'Kaartnummer[ :] 1234-56XX-XXXX-7890  TOM VAN DER MEULEN'  card
           '16/03 17/03  ACME MARKT GLASGOW GBR  [10,80-USD]  10,00 -'  purchase or refund -> row
           '1 EUR=1,0800000 USD'                            rate of the line above
           'GBR'                                            rest of the description above
           'OVERDRACHT NAAR BANKREKENING 123-1234567-12  30,00 -'  credit paid out to an account -> row
           'Refund of First presentment -  30,00 +'         reversal of an earlier payment -> row
           'KOSTEN OP GELDOPNEMING  5,00 -'                 fee of the cash withdrawal on the line
                                                            above -> row, with that line's dates;
                                                            the line above gets Geldopneming = 1
           'Subtotaal [DHR. TOM VAN DER MEULEN]  42,50 -'   end of the holder section, check only
Anything else fails the whole statement, so a missed or misread line cannot pass unnoticed.

Checks, each failing the whole statement: the header is the same on every page and numbers the
pages; previous balance + repayments + holder totals = new balance; per holder section: the
sum of its lines = its Subtotaal = its total on page 1, under the same name.

Row source line: 'p<page>/<line>', line = place among the lines under the column titles, from 1.

Usage: python -m engine.banks.argenta_mastercard.statement <pdf>   (every line, its kind, the rows)
"""

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from engine.banks.argenta_mastercard.parsers import (
    FEE_TEXT,
    RE_TRANSFER_OUT,
    REPAYMENT_TEXTS,
    REVERSAL_TEXT,
    parse_amount,
    parse_signed,
)
from engine.core.pdf_text import TextPiece, read_pdf_text

COLUMNS = [
    "Klantenreferentie",
    "Afschriftdatum",
    "Volgnummer",
    "Datum transactie",
    "Datum verrekening",
    "Omschrijving",
    "Bedrag",
    "Munt",
    "Origineel bedrag",
    "Originele munt",
    "Koers",
    "Kaartnummer",
    "Kaarthouder",
    "Geldopneming",  # "1" on the line a KOSTEN OP GELDOPNEMING line follows; not printed as such
]

# Pieces of one line differ in height by up to about 1 pt; lines are at least 9 pt apart
LINE_TOLERANCE = 1.5
# Column titles are printed tiny; everything above them is the page header
TITLE_MAX_SIZE = 5.0
COLUMN_TITLES = {"DATUM", "TRANSACTIE", "VERREKENING", "OMSCHRIJVING", "BEDRAG", "BEDRAG (EUR)"}
# Older generations print no currency in the title; their rates ('1 EUR=…') show it is EUR
DEFAULT_CURRENCY = "EUR"
# Header right of this x: the page number on the 'Datum' line; elsewhere the address block and
# notices, which say nothing about the transactions
HEADER_RIGHT_X = 200.0
# Column bands: (name, x from); a band runs to the next one
BANDS = [
    ("transaction_date", 0.0),
    ("settlement_date", 60.0),
    ("description", 140.0),
    ("foreign", 300.0),
    ("amount", 480.0),
    ("sign", 552.0),
]

RE_DAY_MONTH = re.compile(r"^[0-9]{2}/[0-9]{2}$")
RE_AMOUNT = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}$")
# The foreign amount's own sign is always '-', also on refunds: only the EUR sign counts
RE_FOREIGN = re.compile(r"^([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}) ?- ?([A-Z]{3})$")
RE_RATE = re.compile(r"^1 EUR=([0-9]+,[0-9]+) ([A-Z]{3})$")
RE_CARD_NUMBER = r"[0-9]{4}-[0-9]{2}XX-XXXX-[0-9]{4}"

RE_HEADER_DATE = re.compile(r"^Datum ([0-9]{2}/[0-9]{2}/[0-9]{2}(?:[0-9]{2})?)$")
RE_PAGE_NUMBER = re.compile(r"^([0-9]+) ?/ ?([0-9]+)$")
RE_HEADER_PERIOD = re.compile(r"^Transacties van ([0-9/]+) tot ([0-9/]+)$")
RE_HEADER_CUSTOMER = re.compile(r"^Klantenreferentie ([0-9]+)$")
# Credit limit and the bank's name: about the card, not about a transaction
RE_HEADER_DROPPED = re.compile(r"^(?:Gebruikslimiet .+ EUR|ARGENTA)$")

RE_PREVIOUS_BALANCE = re.compile(r"^Vorig saldo van ([0-9]{2}/[0-9]{2}/[0-9]{4})$")
RE_NEW_BALANCE = re.compile(r"^Nieuw [Ss]aldo( in uw voordeel)?$")
IN_FAVOUR = "in uw voordeel"
PAYMENT_NOTICE = "Betaling door uw financiële instelling"
RE_HOLDER = re.compile(r"^Naam: (.+)$")
RE_CARD = re.compile(rf"^Kaartnummer(?: :)? ({RE_CARD_NUMBER}) (.+)$")
RE_SUBTOTAL = re.compile(r"^Subtotaal(?: (.+))?$")
# What a continuation line may follow
CARD_LINE_KINDS = ("purchase", "refund", "reversal", "cash withdrawal", "transfer to bank account", "continuation")


@dataclass
class Line:
    page: int
    number: int  # place under the column titles, from 1
    pieces: list[TextPiece]
    transaction_date: str = ""
    settlement_date: str = ""
    texts: list[tuple[str, str]] = field(default_factory=list)  # (band, text)
    amount: str = ""
    sign: str = ""
    foreign: tuple[str, str] | None = None  # (amount, currency)
    rate: tuple[str, str] | None = None  # (rate, currency)
    kind: str = ""

    @property
    def where(self) -> str:
        return f"p{self.page}/{self.number}"

    @property
    def text(self) -> str:
        """The texts joined, whitespace collapsed: for matching labels."""
        return " ".join(" ".join(text for _, text in self.texts).split())

    def only_text(self) -> str | None:
        """The text when the line holds nothing else, else None."""
        if self.transaction_date or self.settlement_date or self.amount or self.sign or self.foreign or self.rate:
            return None
        return self.text

    def fail(self, message: str) -> ValueError:
        shown = " | ".join(piece.text for piece in sorted(self.pieces, key=lambda p: p.x))
        return ValueError(f"Statement line {self.where} ({shown}): {message}")


@dataclass
class Section:
    """What one Subtotaal closes: a card holder's lines, of one or more cards."""

    holder: str = ""  # titled name ('DHR. …') from 'Naam:' or the Subtotaal
    card_number: str = ""
    card_holder: str = ""
    subtotal: Decimal = Decimal(0)
    rows: list[dict] = field(default_factory=list)


def band_of(x: float) -> str:
    name = BANDS[0][0]
    for band, start in BANDS:
        if x >= start:
            name = band
    return name


def group_lines(pieces: list[TextPiece]) -> list[list[TextPiece]]:
    """Pieces of one page into lines, top to bottom, each sorted by x."""
    lines: list[list[TextPiece]] = []
    top = None
    for piece in sorted(pieces, key=lambda p: (-p.y, p.x)):
        if top is None or piece.y < top - LINE_TOLERANCE:
            lines.append([])
            top = piece.y
        lines[-1].append(piece)
    return [sorted(line, key=lambda p: p.x) for line in lines]


def split_line(page: int, number: int, pieces: list[TextPiece]) -> Line:
    line = Line(page, number, pieces)
    for piece in pieces:
        band, text = band_of(piece.x), piece.text.strip()
        if band in ("transaction_date", "settlement_date") and RE_DAY_MONTH.match(text):
            if getattr(line, band):
                raise line.fail(f"two dates in the {band} column")
            setattr(line, band, text)
        elif band == "amount" and RE_AMOUNT.match(text):
            if line.amount:
                raise line.fail("two amounts")
            line.amount = text
        elif band == "sign" and text in ("-", "+"):
            if line.sign:
                raise line.fail("two signs")
            line.sign = text
        elif band == "foreign" and RE_FOREIGN.match(text):
            line.foreign = RE_FOREIGN.match(text).groups()
        elif band == "foreign" and RE_RATE.match(text):
            line.rate = RE_RATE.match(text).groups()
        else:
            line.texts.append((band, piece.text))
    if line.foreign and line.rate:
        raise line.fail("foreign amount and rate on one line")
    return line


def signed_amount(line: Line) -> Decimal:
    """The line's amount with its sign ('-' = owed); only 0,00 may be printed without a sign."""
    if not line.amount:
        raise line.fail("no amount")
    amount = parse_amount(line.amount)
    if not line.sign:
        if amount:
            raise line.fail("amount without a sign")
        return amount
    return -amount if line.sign == "-" else amount


def parse_printed_date(text: str) -> date:
    """'04/04/16' or '04/04/2016' -> date."""
    day, month, year = text.split("/")
    return date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day))


def resolve_day_month(day_month: str, period_end: date) -> str:
    """'26/12' -> the latest such date not after the period end, ISO (a statement can cross New Year)."""
    day, month = (int(part) for part in day_month.split("/"))
    for year in range(period_end.year, period_end.year - 5, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # 29/02 outside a leap year
        if candidate <= period_end:
            return candidate.isoformat()
    raise ValueError(f"Statement date '{day_month}' is no date")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Header:
    statement_date: date
    page: int
    page_count: int
    period: tuple[date, date]
    customer_reference: str


def read_header(page: int, pieces: list[TextPiece]) -> Header:
    found: dict[str, re.Match] = {}
    for line in group_lines(pieces):
        left = " ".join(" ".join(p.text for p in line if p.x < HEADER_RIGHT_X).split())
        right = " ".join(" ".join(p.text for p in line if p.x >= HEADER_RIGHT_X).split())
        if not left or RE_HEADER_DROPPED.match(left):
            continue
        if RE_HEADER_DATE.match(left):
            if not RE_PAGE_NUMBER.match(right):
                raise ValueError(f"Statement page {page}: no page number n/N next to '{left}'")
            found["page"] = RE_PAGE_NUMBER.match(right)
        for name, pattern in (("date", RE_HEADER_DATE), ("period", RE_HEADER_PERIOD), ("customer", RE_HEADER_CUSTOMER)):
            match = pattern.match(left)
            if match:
                if name in found:
                    raise ValueError(f"Statement page {page}: header line '{left}' twice")
                found[name] = match
                break
        else:
            raise ValueError(f"Statement page {page}: unknown header line '{left}'")
    missing = [name for name in ("date", "period", "customer") if name not in found]
    if missing:
        raise ValueError(f"Statement page {page}: header without {', '.join(missing)}")
    try:
        period = (parse_printed_date(found["period"].group(1)), parse_printed_date(found["period"].group(2)))
        statement_date = parse_printed_date(found["date"].group(1))
    except ValueError:
        raise ValueError(f"Statement page {page}: header holds no valid date") from None
    return Header(
        statement_date,
        int(found["page"].group(1)),
        int(found["page"].group(2)),
        period,
        found["customer"].group(1),
    )


def split_page(page: int, pieces: list[TextPiece]) -> tuple[Header, str, list[Line]]:
    """Header, account currency (from the column title) and body lines of one page."""
    titles = [p for p in pieces if p.size < TITLE_MAX_SIZE]
    unknown_titles = sorted({p.text for p in titles} - COLUMN_TITLES)
    if not titles or unknown_titles:
        raise ValueError(f"Statement page {page}: column titles missing or unknown: {unknown_titles}")
    currency_titles = {re.match(r"^BEDRAG \((.+)\)$", p.text) for p in titles} - {None}
    currency = currency_titles.pop().group(1) if currency_titles else DEFAULT_CURRENCY
    titles_bottom = min(p.y for p in titles)
    header = read_header(page, [p for p in pieces if p.size >= TITLE_MAX_SIZE and p.y > titles_bottom])
    body = [p for p in pieces if p.y < titles_bottom and p.size >= TITLE_MAX_SIZE]
    lines = [split_line(page, number, line) for number, line in enumerate(group_lines(body), start=1)]
    return header, currency, lines


# ---------------------------------------------------------------------------
# Statement
# ---------------------------------------------------------------------------
def is_statement(pieces: list[TextPiece]) -> bool:
    first_page = [p.text for p in pieces if p.page == 1]
    return any(t.startswith("Klantenreferentie") for t in first_page) and any(
        t.startswith("Transacties van") for t in first_page
    )


def read(path: str) -> tuple[list[dict[str, str]], list[str], tuple[str, str]] | None:
    """(rows, source lines, printed period) of a statement; None for a PDF that is not one."""
    try:
        pieces = read_pdf_text(path)
    except ValueError:
        # Argenta's generator names itself in the unencrypted document info
        with open(path, "rb") as file:
            if b"www.macro4.com" in file.read():
                raise
        return None
    if not is_statement(pieces):
        return None
    rows, source_lines, period, _ = read_statement(pieces)
    return rows, source_lines, period


def read_statement(
    pieces: list[TextPiece],
) -> tuple[list[dict[str, str]], list[str], tuple[str, str], list[Line]]:
    """Rows, their source lines, the printed period (ISO) and every classified line."""
    page_numbers = sorted({p.page for p in pieces})
    pages = [split_page(page, [p for p in pieces if p.page == page]) for page in page_numbers]
    first = pages[0][0]
    for page, (header, currency, _) in zip(page_numbers, pages, strict=True):
        if (header.statement_date, header.period, header.customer_reference) != (
            first.statement_date,
            first.period,
            first.customer_reference,
        ):
            raise ValueError(f"Statement page {page}: header differs from page 1")
        if (header.page, header.page_count) != (page, len(pages)):
            raise ValueError(f"Statement page {page} is numbered {header.page}/{header.page_count}")
        if currency != pages[0][1]:
            raise ValueError(f"Statement page {page}: currency {currency}, page 1 {pages[0][1]}")
    period_end = first.period[1]

    def day(day_month: str) -> str:
        return resolve_day_month(day_month, period_end) if day_month else ""

    def row(line: Line, section: Section | None, **values: str) -> dict:
        return {
            "Klantenreferentie": first.customer_reference,
            "Afschriftdatum": first.statement_date.isoformat(),
            "Volgnummer": "",  # numbered at the end, in statement order
            "Datum transactie": day(line.transaction_date),
            "Datum verrekening": day(line.settlement_date),
            "Omschrijving": line.texts[0][1].rstrip() if line.texts else "",
            "Bedrag": f"{line.amount}{line.sign}",
            "Munt": pages[0][1],
            "Origineel bedrag": "",
            "Originele munt": "",
            "Koers": "",
            "Kaartnummer": section.card_number if section else "",
            "Kaarthouder": (section.card_holder or section.holder) if section else "",
            "Geldopneming": "",
            "_line": line,
            **values,
        }

    rows: list[dict] = []
    all_lines: list[Line] = []
    summary = {"previous": [], "new": [], "repayments": [], "totals": []}
    sections: list[Section] = []
    section = Section()
    previous: Line | None = None

    for page, (_, _, lines) in zip(page_numbers, pages, strict=True):
        for line in lines:
            all_lines.append(line)
            text, only = line.text, line.only_text()
            no_dates = not line.transaction_date and not line.settlement_date
            one_description = [band for band, _ in line.texts] == ["description"]
            if page == 1:
                if RE_PREVIOUS_BALANCE.match(text) and no_dates and not line.foreign:
                    line.kind = "previous balance"
                    summary["previous"].append(signed_amount(line))
                elif text in REPAYMENT_TEXTS and one_description and line.transaction_date and line.sign:
                    line.kind = "repayment"
                    rows.append(row(line, None))
                    summary["repayments"].append(signed_amount(line))
                elif RE_NEW_BALANCE.match(text) and no_dates and not line.foreign:
                    line.kind = "new balance"
                    in_favour = bool(RE_NEW_BALANCE.match(text).group(1))
                    summary["new"].append([signed_amount(line), in_favour, line])
                elif only == IN_FAVOUR and previous is not None and previous.kind == "new balance":
                    line.kind = "in favour"
                    summary["new"][-1][1] = True
                elif only == PAYMENT_NOTICE:
                    line.kind = "payment notice (dropped)"
                elif no_dates and line.amount and not line.foreign and one_description:
                    line.kind = "holder total"
                    summary["totals"].append((text, signed_amount(line), line))
                else:
                    raise line.fail("unknown line on the summary page")
            elif RE_HOLDER.match(text) and only is not None:
                line.kind = "holder"
                if section.holder or section.rows:
                    raise line.fail("second card holder before a Subtotaal")
                section.holder = RE_HOLDER.match(text).group(1)
            elif RE_CARD.match(text) and only is not None:
                line.kind = "card"
                section.card_number, section.card_holder = RE_CARD.match(text).groups()
            elif RE_SUBTOTAL.match(text) and no_dates and not line.foreign:
                line.kind = "subtotal"
                name = RE_SUBTOTAL.match(text).group(1) or ""
                if name and section.holder and name != section.holder:
                    raise line.fail(f"Subtotaal of '{name}' closes the lines of '{section.holder}'")
                section.holder = section.holder or name
                if not section.holder or not section.rows:
                    raise line.fail("Subtotaal without a card holder or without lines")
                total = sum((parse_signed(r["Bedrag"]) for r in section.rows), Decimal(0))
                if total != signed_amount(line):
                    raise line.fail(f"Subtotaal {signed_amount(line)}, its lines add up to {total}")
                section.subtotal = signed_amount(line)
                sections.append(section)
                section = Section()
            elif line.transaction_date and line.settlement_date and line.sign and one_description:
                if not section.card_holder and not section.holder:
                    raise line.fail("card line without a card holder")
                if RE_TRANSFER_OUT.match(text):
                    line.kind = "transfer to bank account"
                elif line.texts[0][1].strip() == REVERSAL_TEXT:
                    line.kind = "reversal"
                else:
                    line.kind = "purchase" if line.sign == "-" else "refund"
                foreign = dict(zip(("Origineel bedrag", "Originele munt"), line.foreign or (), strict=False))
                section.rows.append(row(line, section, **foreign))
            elif line.rate and not line.texts and not line.amount and not line.sign and no_dates:
                line.kind = "rate"
                target = section.rows[-1] if section.rows else None
                if target is None or not target["Originele munt"] or target["Koers"]:
                    raise line.fail("rate without a foreign amount on the line above")
                if line.rate[1] != target["Originele munt"]:
                    raise line.fail(f"rate in {line.rate[1]} for an amount in {target['Originele munt']}")
                target["Koers"] = line.rate[0]
            elif text == FEE_TEXT and one_description and no_dates and line.amount and not line.foreign:
                line.kind = "fee"
                target = section.rows[-1] if section.rows else None
                above = target["_line"] if target else None
                if above is None or above.kind != "purchase":
                    raise line.fail("fee without a card payment above it in its section")
                above.kind = "cash withdrawal"
                target["Geldopneming"] = "1"
                dates = {"Datum transactie": above.transaction_date, "Datum verrekening": above.settlement_date}
                section.rows.append(row(line, section, **{name: day(value) for name, value in dates.items()}))
            elif only and one_description and previous is not None:
                if previous.kind not in CARD_LINE_KINDS or not section.rows:
                    raise line.fail("text line that continues no card line")
                line.kind = "continuation"
                target = section.rows[-1]
                target["Omschrijving"] = f"{target['Omschrijving'].rstrip()} {line.texts[0][1].strip()}"
            else:
                raise line.fail("unknown line")
            previous = line

    # ----- statement checks -----
    if section.holder or section.card_number or section.rows:
        raise ValueError("Statement: card lines after the last Subtotaal")
    for row_values in [r for s in sections for r in s.rows]:
        if row_values["Originele munt"] and not row_values["Koers"]:
            raise row_values["_line"].fail("foreign amount without a rate line")
    if len(summary["previous"]) > 1 or len(summary["new"]) != 1:
        raise ValueError("Statement: page 1 needs at most one 'Vorig saldo' and exactly one 'Nieuw saldo'")
    # Older generations leave out a previous balance of 0
    previous_balance = summary["previous"][0] if summary["previous"] else Decimal(0)
    new_balance, in_favour, new_line = summary["new"][0]
    if in_favour != (new_balance > 0):
        raise new_line.fail("'in uw voordeel' does not match the sign of the new balance")
    calculated = previous_balance + sum(summary["repayments"], Decimal(0))
    calculated += sum((amount for _, amount, _ in summary["totals"]), Decimal(0))
    if calculated != new_balance:
        raise ValueError(
            f"Statement: previous balance + repayments + holder totals = {calculated}, new balance {new_balance}"
        )
    if len(sections) != len(summary["totals"]):
        raise ValueError(f"Statement: {len(summary['totals'])} holder totals on page 1, {len(sections)} Subtotaal")
    for done, (name, amount, total_line) in zip(sections, summary["totals"], strict=True):
        if name != done.holder or amount != done.subtotal:
            raise total_line.fail(f"page 1 total differs from the Subtotaal of '{done.holder}' ({done.subtotal})")

    rows += [r for s in sections for r in s.rows]
    width = max(2, len(str(len(rows))))
    source_lines = []
    for sequence, row_values in enumerate(rows, start=1):
        row_values["Volgnummer"] = f"{sequence:0{width}d}"
        source_lines.append(row_values.pop("_line").where)
    period = (first.period[0].isoformat(), first.period[1].isoformat())
    return rows, source_lines, period, all_lines


def main() -> int:
    """Print every line of one statement with its kind, then the rows."""
    pieces = read_pdf_text(sys.argv[1])
    if not is_statement(pieces):
        print("not an Argenta Mastercard statement")
        return 1
    try:
        rows, source_lines, period, lines = read_statement(pieces)
    except ValueError as exc:
        print(f"FAILED: {exc}")
        return 1
    for line in lines:
        shown = " | ".join(piece.text.strip() for piece in line.pieces)
        print(f"{line.where:<7} {line.kind:<26} {shown}")
    print(f"\nperiod {period[0]} to {period[1]}, {len(rows)} rows")
    for where, row_values in zip(source_lines, rows, strict=True):
        print(f"{where:<7} " + " | ".join(f"{k}={v}" for k, v in row_values.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
