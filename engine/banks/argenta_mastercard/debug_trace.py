"""
Argenta Mastercard's part of engine.debug_row: the line's kind and the rate check. How the
statement was read into rows (every line, its kind, the checks):
  python -m engine.banks.argenta_mastercard.statement <pdf>
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from engine.banks.argenta_mastercard.normalize import line_kind
from engine.banks.argenta_mastercard.parsers import parse_amount, parse_signed


def show_trace(row: dict) -> None:
    try:
        amount = parse_signed(row["amount"])
    except (InvalidOperation, ValueError, IndexError):
        print(f"-- amount {row['amount']!r} unreadable")
        return
    kind = line_kind(row["statement_text"].strip(), amount, row.get("is_cash_withdrawal", ""))
    print(f"-- kind: {kind} (amount {amount})")
    if not row.get("exchange_rate"):
        return
    try:
        foreign = parse_amount(row["foreign_amount"])
        in_eur = (foreign / parse_amount(row["exchange_rate"])).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except (InvalidOperation, ZeroDivisionError):
        print("-- rate check: foreign amount or rate unreadable")
        return
    verdict = "matches" if in_eur == abs(amount) else "MISMATCH, the row fails"
    print(
        f"-- rate check: {foreign} {row['foreign_currency_code']} / {row['exchange_rate']} "
        f"= {in_eur} EUR, line {abs(amount)}: {verdict}"
    )
