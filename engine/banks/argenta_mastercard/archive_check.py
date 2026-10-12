"""
Card statements against the account that pays them, over the archive (run by engine.archive_checks):
no single file holds both sides. The statements are in
archive/originals/argenta_mastercard/<customer reference>/, the monthly debits ('Debet ten voordele
van BCC') in the Argenta account exports under archive/originals/argenta/<IBAN>/.

A card is a credit_card entry of archive/accounts.yaml: its account_number is the customer
reference, its iban the collection account the debits carry as counterparty. No accounts file, or
no card entry with archived statements: nothing to check.

Checks per card:
  - the new balance of a statement that owes money = its debit, about ten days later. A debit names
    its statement by the day number of the statement date ('MASTERCARD  247'); one with an empty
    name (old rows) is matched by amount within MAX_STATEMENT_TO_DEBIT_DAYS;
  - every debit since the first statement has its statement, for the amount it owes;
  - the previous balance of a statement = the new balance of the one before when their periods
    touch or overlap. After a gap (a month without card activity has no statement) it is 0, as the
    debit paid it off, unless the balance before the gap was in the holder's favour: nothing was
    debited, so it carries over;
  - each repayment line ('DOMICILIERING VIA UW BANK', +) is the other side of one debit: same
    amount, within CARD_REPAYMENT_MATCH_DAYS of the line's settlement date (the oldest layouts print
    only one date). A debit's repayment line is on the statement after its own; after a gap that
    statement was never sent, so the debit has none.
Skipped: debits for statements older than the first archived one (those are missing), and a
statement whose debit may still come after the newest row of the paying account's exports.
Exports overlap: a row is taken once by the bank's duplicate key, a statement once by its date.
"""

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from engine.banks.argenta.normalize import RE_CARD_SETTLEMENT_NAME
from engine.banks.argenta.parsers import (
    MAX_STATEMENT_TO_DEBIT_DAYS,
    parse_exact_amount,
    statement_date_from_day_number,
)
from engine.banks.argenta_mastercard.parsers import REPAYMENT_TEXTS, parse_signed
from engine.banks.argenta_mastercard.statement import Statement, read_file
from engine.core.csv_runtime import load_all_bank_configs, load_rows
from engine.core.csv_validation import extract_duplicate_key, validate_and_prepare
from engine.core.runtime import BASE_DIR
from engine.firefly.accounts import (
    CARD_REPAYMENT_MATCH_DAYS,
    ROLES,
    AccountsFileError,
    OwnAccount,
    clean_identifier,
    load_accounts_file,
)

CARD_BANK = "argenta_mastercard"
ACCOUNT_BANK = "argenta"
DEBIT_TYPE = "Debet ten voordele van BCC"


@dataclass(frozen=True)
class Debit:
    account: str  # IBAN of the paying account
    booking_date: str
    amount: Decimal
    reference: str
    name: str
    source: str  # file and row

    def describe(self) -> str:
        name = f" '{self.name}'" if self.name else " (no name)"
        return f"debit of {self.booking_date} {self.amount}{name} on {self.account} ({self.reference}, {self.source})"


@dataclass(frozen=True)
class ArchivedStatement:
    statement: Statement
    file: str

    def describe(self) -> str:
        return f"statement of {self.statement.statement_date} ({self.file})"


@dataclass(frozen=True)
class Repayment:
    archived: ArchivedStatement
    main_date: str  # settlement date, else the only date printed (oldest layouts)
    amount: Decimal
    where: str  # page and line

    def describe(self) -> str:
        return f"repayment line {self.amount} of {self.main_date} ({self.where}) on the {self.archived.describe()}"


def days_between(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def check_archive(archive_dir: str) -> list[str]:
    accounts_path = os.path.join(archive_dir, "accounts.yaml")
    if not os.path.isfile(accounts_path):
        return []
    try:
        accounts = load_accounts_file(accounts_path)
    except AccountsFileError as exc:
        return [f"{accounts_path} is invalid, card statements not checked: {exc}"]

    differences = []
    for card in accounts:
        folder = os.path.join(archive_dir, "originals", CARD_BANK, card.account_number)
        if card.role == ROLES["credit_card"] and card.account_number and os.path.isdir(folder):
            differences += [f"{card.name}: {text}" for text in check_card(archive_dir, card, folder)]
    return differences


def check_card(archive_dir: str, card: OwnAccount, folder: str) -> list[str]:
    statements, differences = read_statements(folder)
    differences += check_balance_chain(statements)
    if not statements:
        return differences
    if not card.iban:
        return [*differences, "no iban in accounts.yaml: its debits on the current account cannot be found"]
    debits, newest_rows, unreadable = read_debits(archive_dir, card.iban)
    differences += unreadable

    by_date = {s.statement.statement_date: s for s in statements}
    first_date = statements[0].statement.statement_date
    paid_by: defaultdict[str, list[Debit]] = defaultdict(list)
    unnamed = []
    for debit in debits:
        if not debit.name:
            if debit.booking_date >= first_date:
                unnamed.append(debit)
            continue
        match = RE_CARD_SETTLEMENT_NAME.match(debit.name)
        if not match:
            differences.append(f"{debit.describe()}: name in an unknown form")
            continue
        try:
            statement_date = statement_date_from_day_number(match.group(1), debit.booking_date)
        except ValueError as exc:
            differences.append(f"{debit.describe()}: {exc}")
            continue
        if statement_date < first_date:
            continue
        if statement_date not in by_date:
            differences.append(f"{debit.describe()} pays the statement of {statement_date}, which is not archived")
            continue
        paid_by[statement_date].append(debit)
    for debit in unnamed:
        candidates = [
            s.statement.statement_date
            for s in statements
            if s.statement.statement_date not in paid_by
            and s.statement.new_balance == debit.amount
            and 0 <= days_between(s.statement.statement_date, debit.booking_date) <= MAX_STATEMENT_TO_DEBIT_DAYS
        ]
        if candidates:
            paid_by[candidates[-1]].append(debit)
        else:
            differences.append(
                f"{debit.describe()}: no archived statement with that new balance in the "
                f"{MAX_STATEMENT_TO_DEBIT_DAYS} days before"
            )

    owing = [s for s in statements if s.statement.new_balance < 0]
    if owing and not debits:
        differences.append(
            f"no Argenta account export in the archive holds a '{DEBIT_TYPE}' to {card.iban}: "
            "the statements cannot be checked against their debits"
        )
        return differences
    newest = max(newest_rows[debit.account] for debit in debits) if debits else ""
    for archived in statements:
        statement = archived.statement
        found = paid_by.get(statement.statement_date, [])
        if len(found) > 1:
            listed = "; ".join(debit.describe() for debit in found)
            differences.append(f"{archived.describe()} is paid by {len(found)} debits: {listed}")
        elif found and found[0].amount != statement.new_balance:
            differences.append(f"{archived.describe()}: new balance {statement.new_balance}, but {found[0].describe()}")
        elif not found and statement.new_balance < 0:
            if days_between(statement.statement_date, newest) < MAX_STATEMENT_TO_DEBIT_DAYS:
                continue  # its debit may still come
            differences.append(
                f"{archived.describe()}: new balance {statement.new_balance}, but no debit on the account "
                f"in the {MAX_STATEMENT_TO_DEBIT_DAYS} days after"
            )
    return differences + check_repayments(statements, debits, paid_by)


def check_repayments(
    statements: list[ArchivedStatement], debits: list[Debit], paid_by: dict[str, list[Debit]]
) -> list[str]:
    """Each repayment line against the debit it is the other side of, one to one."""
    repayments = [
        Repayment(archived, row["Datum verrekening"] or row["Datum transactie"], parse_signed(row["Bedrag"]), where)
        for archived in statements
        for row, where in zip(archived.statement.rows, archived.statement.source_lines, strict=True)
        if " ".join(row["Omschrijving"].split()) in REPAYMENT_TEXTS
    ]
    differences = []
    claimed: set[Debit] = set()
    for repayment in sorted(repayments, key=lambda r: r.main_date):
        candidates = [
            debit
            for debit in debits
            if debit not in claimed
            and debit.amount == -repayment.amount
            and abs(days_between(debit.booking_date, repayment.main_date)) <= CARD_REPAYMENT_MATCH_DAYS
        ]
        if candidates:
            claimed.add(min(candidates, key=lambda d: abs(days_between(d.booking_date, repayment.main_date))))
        else:
            differences.append(
                f"{repayment.describe()}: no debit of {-repayment.amount} on the account within "
                f"{CARD_REPAYMENT_MATCH_DAYS} days"
            )
    following = {
        before.statement.statement_date: after
        for before, after in zip(statements, statements[1:], strict=False)
        if follows(before, after)
    }
    for statement_date, found in paid_by.items():
        after = following.get(statement_date)
        # None: no later statement yet, or a gap, whose statement holding the repayment was never sent
        if after is None:
            continue
        differences += [
            f"{debit.describe()}: no repayment line of {-debit.amount} on the {after.describe()}"
            for debit in found
            if debit not in claimed
        ]
    return differences


def follows(before: ArchivedStatement, after: ArchivedStatement) -> bool:
    """The periods touch or overlap: no statement is missing between them."""
    last_day = date.fromisoformat(before.statement.period[1])
    return date.fromisoformat(after.statement.period[0]) <= last_day + timedelta(days=1)


def check_balance_chain(statements: list[ArchivedStatement]) -> list[str]:
    differences = []
    for before, after in zip(statements, statements[1:], strict=False):
        touching = follows(before, after)
        # After a gap the debit paid an owed balance off; a balance in the holder's favour stays
        carried = before.statement.new_balance if touching or before.statement.new_balance > 0 else Decimal("0.00")
        if after.statement.previous_balance != carried:
            gap = "" if touching else f" (no statement between them: expected {carried})"
            differences.append(
                f"{after.describe()}: previous balance {after.statement.previous_balance}, but the "
                f"{before.describe()} ends at {before.statement.new_balance}{gap}"
            )
    return differences


def read_statements(folder: str) -> tuple[list[ArchivedStatement], list[str]]:
    """Every statement in the folder once, oldest first; differences for what cannot be read."""
    by_date: dict[str, ArchivedStatement] = {}
    differences = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".pdf"):
            continue
        try:
            statement = read_file(os.path.join(folder, name))
        except ValueError as exc:
            differences.append(f"archived statement {name} cannot be read: {exc}")
            continue
        if statement is None:
            differences.append(f"archived file {name} is no card statement")
            continue
        archived = ArchivedStatement(statement, name)
        earlier = by_date.setdefault(statement.statement_date, archived)
        balances = (statement.previous_balance, statement.new_balance)
        if balances != (earlier.statement.previous_balance, earlier.statement.new_balance):
            differences.append(f"{archived.describe()} and {earlier.file}: same date, other balances")
    return [by_date[key] for key in sorted(by_date)], differences


def read_debits(archive_dir: str, card_iban: str) -> tuple[list[Debit], dict[str, str], list[str]]:
    """
    The debits to the card in every Argenta account export, each once; the newest booking date per
    account; differences for what cannot be read.
    """
    config = load_all_bank_configs(os.path.join(BASE_DIR, "config"))[ACCOUNT_BANK]
    root = os.path.join(archive_dir, "originals", ACCOUNT_BANK)
    seen: set[tuple[str, str | None]] = set()
    debits: list[Debit] = []
    newest: dict[str, str] = {}
    differences = []
    for account_folder in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, account_folder)
        for name in sorted(os.listdir(folder)):
            try:
                rows, source_lines, _ = load_rows(os.path.join(folder, name), {ACCOUNT_BANK: config})
                prepared, _, _ = validate_and_prepare(rows, config, source_lines)
            except ValueError as exc:
                differences.append(f"archived export {account_folder}/{name} cannot be read: {exc}")
                continue
            for row in prepared:
                if "_validation_error" in row:
                    continue
                account = clean_identifier(row["asset_account_iban"])
                newest[account] = max(newest.get(account, ""), row["primary_transaction_date"])
                key = (account, extract_duplicate_key(row, config))
                if key in seen:
                    continue
                seen.add(key)
                to_card = clean_identifier(row["opposing_account_iban"]) == card_iban
                if row["transaction_type"] == DEBIT_TYPE and to_card:
                    debits.append(
                        Debit(
                            account=account,
                            booking_date=row["primary_transaction_date"],
                            amount=parse_exact_amount(row["amount"]),
                            reference=row["external_id"],
                            name=" ".join(row["opposing_account_name"].split()),
                            source=f"{name} row {row['_source_line']}",
                        )
                    )
    return sorted(debits, key=lambda debit: debit.booking_date), newest, differences
