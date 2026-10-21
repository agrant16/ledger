import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import psycopg

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError
from ledger.infra.config import load_settings
from ledger.models.account_model import Account
from ledger.service.account_service import create_account, find_account_by_name

DEFAULT_CURRENCIES = [
    "USD",
    "EUR",
]


class SeedError(Exception):
    """The seed run cannot continue: the existing data isn't what the script needs."""


def _currency_validator(value: str) -> str:
    """Validates that the value for --currency is an uppercase string of three letters."""
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid format. Must be 3 uppercase letters, e.g. 'USD' or 'EUR'."
        )
    return value


def _per_currency_validator(value: str) -> int:
    """
    Validates the value for --per-currency argument is 1 <= N <= 500.
    """
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--per-currency must be positive integer.")
    integer = int(value)
    if integer > 500:
        raise argparse.ArgumentTypeError("--per-currency must be a positive integer <= 500.")
    return integer


def _set_up_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the database with funding accounts", prog="python -m ledger.seed"
    )
    parser.add_argument(
        "--currency",
        action="append",
        type=_currency_validator,
        help="Currency code to include (e.g., --currency USD --currency EUR). "
        f"Can be repeated. Defaults to {DEFAULT_CURRENCIES}.",
    )
    parser.add_argument(
        "--per-currency",
        type=_per_currency_validator,
        default=1,
        help="Number of records (N) to generate per currency. Must be 1 <= N <= 500",
    )

    # --output is 100% required
    parser.add_argument(
        "-o", "--output", required=True, help="PATH where the output ledger file will be saved."
    )
    return parser


def _create_or_find(
    conn: psycopg.Connection, account_name: str, currency: str
) -> tuple[Account, bool]:
    """Create the funding account, or return the one another run committed first.

    The insert runs in a savepoint: if another run commits the same name first, the unique
    violation would otherwise abort the whole database transaction. After it, the name is looked
    up again. The bool is whether this call created the account.
    """
    try:
        with conn.transaction():
            account = create_account(conn, account_name, currency, allow_negative=True)
    except DuplicateAccountNameError:
        account = find_account_by_name(conn, account_name)
        if account is None:
            raise LedgerInvariantError(
                f"Account {account_name} was a duplicate but cannot be found"
            ) from None
        return account, False
    return account, True


def _ensure_funding_account(
    conn: psycopg.Connection, account_name: str, currency: str
) -> tuple[Account, bool]:
    """The funding account with this name and whether this call created it.

    An existing account is accepted only if it is what we would have created.
    """
    account = find_account_by_name(conn, account_name)
    if account is not None:
        created = False
    else:
        account, created = _create_or_find(conn, account_name, currency)
    if account.currency != currency:
        raise SeedError(
            f"Account {account_name} already exists but currency does not match: "
            f"expected {currency} but account has {account.currency}"
        )
    if not account.allow_negative:
        raise SeedError(f"Account {account_name} already exists but is a protected account")
    return account, created


def _create_accounts(
    conn: psycopg.Connection, currencies: list[str], n: int
) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
    """The ids file content and one report line per account."""
    ids: dict[str, list[int]] = {currency: [] for currency in currencies}
    report: list[str] = []
    for currency in sorted(currencies):
        for number in range(1, n + 1):
            account_name = f"funding:{currency}" if number == 1 else f"funding:{currency}:{number}"
            account, created = _ensure_funding_account(conn, account_name, currency)
            ids[currency].append(account.id)
            report.append(f"{'created' if created else 'exists '} {account_name} (id {account.id})")
    return {"funding_accounts": ids}, report


def _write_file(accounts: dict[str, dict[str, list[int]]], target_path: str) -> None:
    """Writes the resulting accounts to a file at target_path."""
    target = Path(target_path)
    tmp_file = tempfile.NamedTemporaryFile(
        dir=target.resolve().parent, delete=False, mode="w", encoding="utf-8"
    )
    tmp_path = Path(tmp_file.name)
    # file is opened outside try. This is fine here because if either json.dump or replace
    # fail the with tmp_file context block has already closed the file and cleanup is done.
    try:
        with tmp_file:
            json.dump(accounts, tmp_file)
        tmp_path.replace(target_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = _set_up_parser().parse_args(argv)  # None reads sys.argv; a usage error exits with 2
    # dict.fromkeys drops repeated currencies and keeps the order they were given in.
    currencies = list(dict.fromkeys(args.currency or DEFAULT_CURRENCIES))

    try:
        with psycopg.connect(load_settings().app_url, connect_timeout=20) as conn:
            with conn.transaction():
                accounts, report = _create_accounts(conn, currencies, args.per_currency)
        for line in report:
            print(line)
        _write_file(accounts, args.output)
        print(f"seeded {len(report)} funding accounts, ids written to {args.output}")
        return 0
    except (SeedError, psycopg.OperationalError, OSError) as e:
        print(f"Seed failed with error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
