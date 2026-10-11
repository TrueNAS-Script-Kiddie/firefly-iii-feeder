"""
Own accounts file (archive/accounts.yaml): the feeder makes Firefly's asset accounts match it.
Every field is explained in the template, config/accounts.example.yaml.

Applied by the importer at the start of every run that imports, before it reads the asset
accounts, or by hand (import_normalized --accounts-only):
  - each listed account is found in Firefly by its iban, else its account_number: missing ->
    created, a field that differs -> updated. Nothing is deleted;
  - an own account in Firefly that the file does not list is returned, for an alert;
  - corrections are transactions with internal_reference correction|<account>|<sha8>: one in
    Firefly that the file does not hold is deleted (then purged), one missing is created.

The whole file is checked before anything is sent. An invalid file, or an account or correction
Firefly refuses, raises AccountsFileError: the importer handles it as an outage (exit 69).
"""

import collections
import hashlib
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

from engine.firefly.api import FireflyClient

# kind -> Firefly account_role; checking + shared -> SHARED_ROLE
ROLES = {"checking": "defaultAsset", "savings": "savingAsset", "credit_card": "ccAsset"}
SHARED_ROLE = "sharedAsset"
# The only payment plan Firefly knows ("Full payment every month")
CREDIT_CARD_TYPE = "monthlyFull"

ACCOUNT_FIELDS = {
    "name",
    "kind",
    "shared",
    "iban",
    "account_number",
    "monthly_payment_date",
    "opening_balance",
    "opening_balance_date",
    "active",
    "notes",
    "corrections",
}
CORRECTION_FIELDS = {"date", "amount", "counterparty", "description"}
CORRECTION_PREFIX = "correction|"
CORRECTION_NOTE = "Correction from the accounts file"

RE_IBAN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
CENT = Decimal("0.01")


class AccountsFileError(Exception):
    """The file is invalid, or Firefly refused what it asks for: nothing is imported."""


def clean_identifier(value: str | None) -> str:
    """IBAN or account number without spaces, upper case."""
    return (value or "").replace(" ", "").upper()


@dataclass
class Correction:
    date: str
    amount: str  # signed, two decimals
    counterparty: str
    description: str


@dataclass
class OwnAccount:
    name: str
    role: str
    iban: str  # without spaces; "" when none
    account_number: str  # without spaces, as Firefly stores it; "" when none
    monthly_payment_date: str  # ISO; credit cards only, else ""
    opening_balance: str  # signed, two decimals; "" when none (also for 0)
    opening_balance_date: str
    active: bool
    notes: str  # "" when none
    corrections: list[Correction] = field(default_factory=list)

    @property
    def identifier(self) -> str:
        """What names the account in the correction keys: its IBAN, else its account number."""
        return self.iban or self.account_number

    def describe(self) -> str:
        return f"'{self.name}' ({self.identifier})"


def correction_key(account: OwnAccount, correction: Correction) -> str:
    """internal_reference of a correction: any change to it gives another key."""
    content = f"{correction.date}|{correction.amount}|{correction.counterparty}|{correction.description}"
    return f"{CORRECTION_PREFIX}{account.identifier}|{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------
def parse_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a text, got {value!r}")
    return value.strip()


def parse_date(value: Any, what: str) -> str:
    # YAML reads 2016-01-01 as a date; a datetime (with a time) is a date too, so test it first
    if isinstance(value, datetime):
        raise ValueError(f"{what} must be a date without a time, got {value!r}")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise ValueError(f"{what} must be a date (YYYY-MM-DD), got {value!r}")


def parse_amount(value: Any, what: str) -> str:
    """Signed, exactly two decimals; at most two in the file."""
    # bool is an int in Python; YAML reads yes/no/true as bool
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{what} must be an amount, got {value!r}")
    try:
        # str() of a float is its shortest form: 1234.56 stays 1234.56
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"{what} must be an amount like -1234.56, got {value!r}") from None
    if not amount.is_finite() or amount != amount.quantize(CENT):
        raise ValueError(f"{what} must be an amount with at most two decimals, got {value!r}")
    return str(amount.quantize(CENT))


def parse_iban(value: Any) -> str:
    iban = clean_identifier(parse_text(value, "iban"))
    rearranged = iban[4:] + iban[:4]
    if not RE_IBAN.match(iban) or int("".join(str(int(c, 36)) for c in rearranged)) % 97 != 1:
        raise ValueError(f"iban {value!r} is not a valid IBAN")
    return iban


def parse_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false, got {value!r}")
    return value


def parse_correction(item: Any) -> Correction:
    if not isinstance(item, dict):
        raise ValueError(f"a correction must be a mapping with {', '.join(sorted(CORRECTION_FIELDS))}, got {item!r}")
    if set(item) != CORRECTION_FIELDS:
        raise ValueError(f"a correction needs exactly {', '.join(sorted(CORRECTION_FIELDS))}, got {item!r}")
    amount = parse_amount(item["amount"], "correction amount")
    if Decimal(amount) == 0:
        raise ValueError(f"correction amount is 0: {item!r}")
    return Correction(
        date=parse_date(item["date"], "correction date"),
        amount=amount,
        counterparty=parse_text(item["counterparty"], "correction counterparty"),
        description=parse_text(item["description"], "correction description"),
    )


def parse_account(item: Any) -> OwnAccount:
    if not isinstance(item, dict):
        raise ValueError(f"must be a mapping of fields, got {item!r}")
    unknown = set(item) - ACCOUNT_FIELDS
    if unknown:
        raise ValueError(f"unknown field(s) {', '.join(sorted(map(str, unknown)))}")

    kind = item.get("kind")
    if kind not in ROLES:
        raise ValueError(f"kind must be one of {', '.join(ROLES)}, got {kind!r}")
    shared = parse_bool(item.get("shared", False), "shared")
    if shared and kind != "checking":
        raise ValueError("shared is only for kind checking")

    iban = parse_iban(item["iban"]) if item.get("iban") is not None else ""
    account_number = ""
    if item.get("account_number") is not None:
        # Unquoted digits are a YAML number, which drops leading zeros
        if not isinstance(item["account_number"], str):
            raise ValueError(f"account_number must be quoted text, got {item['account_number']!r}")
        # Firefly drops the spaces: kept, they would differ from Firefly at every run
        account_number = parse_text(item["account_number"], "account_number").replace(" ", "")

    if not iban and not account_number:
        raise ValueError("needs an iban or an account_number: Firefly's account is found by one of them")

    monthly_payment_date = ""
    if kind == "credit_card":
        if item.get("monthly_payment_date") is None:
            raise ValueError("kind credit_card needs monthly_payment_date")
        monthly_payment_date = parse_date(item["monthly_payment_date"], "monthly_payment_date")
    elif "monthly_payment_date" in item:
        raise ValueError("monthly_payment_date is only for kind credit_card")

    if ("opening_balance" in item) != ("opening_balance_date" in item):
        raise ValueError("opening_balance and opening_balance_date: both or neither")
    opening_balance = opening_balance_date = ""
    if "opening_balance" in item:
        opening_balance = parse_amount(item["opening_balance"], "opening_balance")
        opening_balance_date = parse_date(item["opening_balance_date"], "opening_balance_date")
        # Firefly keeps no opening balance of 0
        if Decimal(opening_balance) == 0:
            opening_balance = opening_balance_date = ""

    corrections = item.get("corrections") or []
    if not isinstance(corrections, list):
        raise ValueError(f"corrections must be a list, got {corrections!r}")

    return OwnAccount(
        name=parse_text(item.get("name"), "name"),
        role=SHARED_ROLE if shared else ROLES[kind],
        iban=iban,
        account_number=account_number,
        monthly_payment_date=monthly_payment_date,
        opening_balance=opening_balance,
        opening_balance_date=opening_balance_date,
        active=parse_bool(item.get("active", True), "active"),
        notes=parse_text(item["notes"], "notes") if item.get("notes") is not None else "",
        corrections=[parse_correction(c) for c in corrections],
    )


def load_accounts_file(path: str) -> list[OwnAccount]:
    """Every account of the file, checked as a whole; AccountsFileError names the account and the reason."""
    try:
        with open(path, encoding="utf-8") as f:
            items = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise AccountsFileError(str(exc)) from None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise AccountsFileError("must be a list of accounts (one '- name: ...' per account)")

    accounts: list[OwnAccount] = []
    for position, item in enumerate(items, start=1):
        label = item.get("name") if isinstance(item, dict) else None
        try:
            accounts.append(parse_account(item))
        except (ValueError, KeyError) as exc:
            raise AccountsFileError(f"account {position} ({label!r}): {exc}") from None

    seen: dict[str, str] = {}
    for account in accounts:
        for key in (
            f"name {account.name}",
            *(f"identifier {i}" for i in {account.iban, clean_identifier(account.account_number)} - {""}),
        ):
            if key in seen:
                raise AccountsFileError(f"accounts '{seen[key]}' and '{account.name}' share the {key}")
            seen[key] = account.name
        keys = [correction_key(account, c) for c in account.corrections]
        if len(keys) != len(set(keys)):
            raise AccountsFileError(f"account '{account.name}' lists the same correction twice")
    return accounts


# ---------------------------------------------------------------------------
# Applying it to Firefly
# ---------------------------------------------------------------------------
@dataclass
class Applied:
    accounts: list[dict[str, Any]]  # Firefly's asset accounts after applying, as GET accounts gives them
    unlisted: list[dict[str, Any]]  # own accounts in Firefly the file does not list
    counts: collections.Counter

    def summary(self) -> str:
        return ", ".join(f"{n} {what}" for what, n in self.counts.items()) or "nothing to change"


def firefly_identifiers(firefly_account: dict[str, Any]) -> set[str]:
    attributes = firefly_account["attributes"]
    return {clean_identifier(attributes.get("iban")), clean_identifier(attributes.get("account_number"))} - {""}


def firefly_notes(attributes: dict[str, Any]) -> str:
    """An account's notes as Firefly stores them: trimmed, with LF line ends."""
    return (attributes.get("notes") or "").replace("\r\n", "\n").strip()


def find_in_firefly(account: OwnAccount, firefly_accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    The Firefly asset account holding the account's IBAN, else its account number (in either
    field).
    """
    for identifier in (account.iban, clean_identifier(account.account_number)):
        if not identifier:
            continue
        hits = [f for f in firefly_accounts if identifier in firefly_identifiers(f)]
        if len(hits) > 1:
            names = ", ".join(f"'{h['attributes']['name']}'" for h in hits)
            raise AccountsFileError(
                f"account {account.describe()}: {identifier} is on several Firefly accounts: {names}"
            )
        if hits:
            return hits[0]
    return None


def wanted_fields(account: OwnAccount) -> dict[str, Any]:
    """What Firefly must hold; None clears a field."""
    fields: dict[str, Any] = {
        "name": account.name,
        "account_role": account.role,
        "iban": account.iban or None,
        "account_number": account.account_number or None,
        "active": account.active,
        "opening_balance": account.opening_balance or None,
        "opening_balance_date": account.opening_balance_date or None,
        "notes": account.notes or None,
    }
    if account.monthly_payment_date:
        fields.update(credit_card_type=CREDIT_CARD_TYPE, monthly_payment_date=account.monthly_payment_date)
    return fields


def current_fields(attributes: dict[str, Any], wanted: dict[str, Any]) -> dict[str, Any]:
    """Firefly's values of the fields in `wanted`, in the same form."""
    opening_balance = attributes.get("opening_balance")
    opening_date = attributes.get("opening_balance_date")
    has_opening = bool(opening_date) and opening_balance is not None and Decimal(opening_balance) != 0
    current = {
        "name": attributes.get("name"),
        "account_role": attributes.get("account_role"),
        "iban": clean_identifier(attributes.get("iban")) or None,
        "account_number": (attributes.get("account_number") or "").strip() or None,
        "active": bool(attributes.get("active")),
        "opening_balance": str(Decimal(opening_balance).quantize(CENT)) if has_opening else None,
        # Firefly answers with a timestamp
        "opening_balance_date": opening_date[:10] if has_opening else None,
        "credit_card_type": attributes.get("credit_card_type"),
        "monthly_payment_date": (attributes.get("monthly_payment_date") or "")[:10] or None,
        "notes": firefly_notes(attributes) or None,
    }
    return {name: current[name] for name in wanted}


def refused(what: str, status: int, response: dict[str, Any]) -> AccountsFileError:
    reason = f"{what}: Firefly refused it, HTTP {status}: {response.get('message', '')} {response.get('errors', '')}"
    if "iban" in str(response.get("errors", "")).lower() or "account_number" in str(response.get("errors", "")):
        # An asset account's IBAN may not be on any expense or revenue account either
        reason += (
            "; an expense or revenue account made by earlier imports may hold that IBAN or number:"
            " start-over.bash removes those before the accounts are applied"
        )
    return AccountsFileError(reason)


def apply_accounts(
    client: FireflyClient, accounts: list[OwnAccount], dry_run: bool, log: Callable[[str], None]
) -> Applied:
    """Make Firefly's asset accounts and corrections match the file. A dry run sends no change."""
    firefly_accounts = client.get_all("accounts?type=asset")
    counts: collections.Counter = collections.Counter()

    matches = [(account, find_in_firefly(account, firefly_accounts)) for account in accounts]
    claimed: dict[str, OwnAccount] = {}
    for account, found in matches:
        if found is None:
            continue
        other = claimed.setdefault(str(found["id"]), account)
        if other is not account:
            raise AccountsFileError(
                f"accounts {other.describe()} and {account.describe()} both match Firefly account "
                f"'{found['attributes']['name']}'"
            )
    unlisted = [f for f in firefly_accounts if str(f["id"]) not in claimed]

    account_ids: dict[str, str] = {}  # file account name -> Firefly id
    after: list[dict[str, Any]] = []
    for account, found in matches:
        wanted = wanted_fields(account)
        if found is None:
            body = {"type": "asset", **{k: v for k, v in wanted.items() if v is not None}}
            if dry_run:
                log(f"Would create account {account.describe()}")
                result = {"id": f"dry-run {account.name}", "attributes": dict(wanted)}
            else:
                status, response = client.request("POST", "accounts", body)
                if status != 200:
                    raise refused(f"creating account {account.describe()}", status, response)
                result = response["data"]
                log(f"Created account {account.describe()}: Firefly id {result['id']}")
            counts["accounts created"] += 1
        else:
            current = current_fields(found["attributes"], wanted)
            changes = [f"{k} {current[k]!r} -> {v!r}" for k, v in wanted.items() if current[k] != v]
            result = found
            if changes:
                # Every field, not only the changed ones: Firefly checks some only together
                # (a credit card's payment date with its role)
                if dry_run:
                    log(f"Would update account {account.describe()}: {'; '.join(changes)}")
                    result = {"id": found["id"], "attributes": {**found["attributes"], **wanted}}
                else:
                    status, response = client.request("PUT", f"accounts/{found['id']}", wanted)
                    if status != 200:
                        raise refused(f"updating account {account.describe()}", status, response)
                    result = response["data"]
                    log(f"Updated account {account.describe()}, Firefly id {found['id']}: {'; '.join(changes)}")
                counts["accounts updated"] += 1
        after.append(result)
        account_ids[account.name] = str(result["id"])

    apply_corrections(client, accounts, account_ids, dry_run, log, counts)
    return Applied(accounts=after + unlisted, unlisted=unlisted, counts=counts)


def apply_corrections(
    client: FireflyClient,
    accounts: list[OwnAccount],
    account_ids: dict[str, str],
    dry_run: bool,
    log: Callable[[str], None],
    counts: collections.Counter,
) -> None:
    """Delete the correction transactions the file no longer holds, then create the missing ones."""
    wanted = {correction_key(a, c): (a, c) for a in accounts for c in a.corrections}
    query = urllib.parse.quote(f"internal_reference_starts:{CORRECTION_PREFIX}")
    existing: collections.defaultdict[str, list[str]] = collections.defaultdict(list)
    for group in client.get_all(f"search/transactions?query={query}"):
        references = {s.get("internal_reference") or "" for s in group["attributes"]["transactions"]}
        for reference in references:
            if reference.startswith(CORRECTION_PREFIX):
                existing[reference].append(str(group["id"]))

    # A correction twice in Firefly keeps one
    to_delete = [(r, g) for r, groups in existing.items() for g in (groups if r not in wanted else groups[1:])]
    for reference, group_id in to_delete:
        if dry_run:
            log(f"Would delete correction {reference} (Firefly id {group_id})")
        else:
            status, response = client.request("DELETE", f"transactions/{group_id}")
            if status not in (204, 404):
                raise refused(f"deleting correction {reference}", status, response)
            log(f"Deleted correction {reference} (Firefly id {group_id})")
        counts["corrections deleted"] += 1
    if to_delete and not dry_run:
        # Firefly's duplicate check includes deleted transactions
        status, response = client.request("DELETE", "data/purge")
        if status != 204:
            raise refused("purging deleted corrections", status, response)

    for reference, (account, correction) in wanted.items():
        if reference in existing:
            continue
        incoming = not correction.amount.startswith("-")
        split = {
            "type": "deposit" if incoming else "withdrawal",
            "date": correction.date,
            "amount": correction.amount.lstrip("-"),
            "description": correction.description,
            "notes": CORRECTION_NOTE,
            "internal_reference": reference,
        }
        if incoming:
            split.update(destination_id=account_ids[account.name], source_name=correction.counterparty)
        else:
            split.update(source_id=account_ids[account.name], destination_name=correction.counterparty)
        what = f"correction {reference} of {account.describe()}: {correction.date} {correction.amount}"
        if dry_run:
            log(f"Would create {what}")
        else:
            body = {
                "error_if_duplicate_hash": True,
                "apply_rules": True,
                "fire_webhooks": True,
                "transactions": [split],
            }
            status, response = client.request("POST", "transactions", body)
            if status != 200 and not (status == 422 and "Duplicate of transaction" in str(response)):
                raise refused(f"creating {what}", status, response)
            log(f"Created {what}")
        counts["corrections created"] += 1
