import argparse
import json
import os
import re
import sys
import tempfile

import psycopg

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError
from ledger.infra.config import load_settings
from ledger.models.account_model import Account
from ledger.service.account_service import create_account, find_account_by_name


class OverrideAppendAction(argparse._AppendAction):
    """
    Like argparse's `append`, but the first use replaces the default list.

    Plain `append` adds to the default, so `--currency GBP` would give USD, EUR and GBP. Here,
    naming any currency replaces the defaults, and only repeats of the option add to the list.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        # Check if the current value in the namespace is still the default list
        if getattr(namespace, self.dest) is self.default:
            # Clear it out so we don't append to the default
            setattr(namespace, self.dest, [])
        # Call the standard append logic
        super().__call__(parser, namespace, values, option_string)


class SeedException(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


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
        action=OverrideAppendAction,
        type=_currency_validator,
        default=["USD", "EUR"],
        help="Currency code to include (e.g., --currency USD --currency EUR). Can be repeated.",
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


def _ensure_funding_account(
    conn: psycopg.Connection, account_name: str, currency: str
) -> tuple[Account, bool]:
    """The funding account with this name and whether this call created it.

    An existing account is accepted only if it is what we would have created. The insert runs in a
    savepoint: if another run commits the same name first, the unique violation would otherwise
    abort the whole database transaction. After it, the name is looked up again.
    """
    account = find_account_by_name(conn, account_name)
    created = False
    if account is None:
        try:
            with conn.transaction():
                account = create_account(conn, account_name, currency, allow_negative=True)
            created = True
        except DuplicateAccountNameError:
            account = find_account_by_name(conn, account_name)
            if account is None:
                raise LedgerInvariantError(
                    f"Account {account_name} was a duplicate but cannot be found"
                ) from None
    if account.currency != currency:
        raise SeedException(
            f"Account {account_name} already exists but currency does not match {account.currency}"
        )
    if not account.allow_negative:
        raise SeedException(f"Account {account_name} already exists but is a protected account")
    return account, created


def _create_accounts(
    conn: psycopg.Connection, currencies: list[str], n: int
) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
    """The ids file content and one report line per account."""
    accounts = {"funding_accounts": {currency: [] for currency in currencies}}
    report: list[str] = []
    currencies.sort()
    for currency in currencies:
        for i in range(0, n):
            account_name = f"funding:{currency}" if i == 0 else f"funding:{currency}:{i + 1}"
            account, created = _ensure_funding_account(conn, account_name, currency)
            accounts["funding_accounts"][currency].append(account.id)
            report.append(f"{'created' if created else 'exists '} {account_name} (id {account.id})")
    return accounts, report


def _write_file(accounts: dict[str, dict[str, list[int]]], target_path: str) -> None:
    target_dir = os.path.dirname(os.path.abspath(target_path))
    tmp_file = tempfile.NamedTemporaryFile(dir=target_dir, delete=False, mode="w", encoding="utf-8")
    try:
        with tmp_file:
            json.dump(accounts, tmp_file)
        os.replace(tmp_file.name, target_path)
    except OSError as e:
        if os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)
        raise e


def main(argv: list[str] | None = None) -> int:
    args = _set_up_parser().parse_args(argv)  # None reads sys.argv; a usage error exits with 2
    # dict.fromkeys drops repeated currencies and keeps the order they were given in.
    currencies = list(dict.fromkeys(args.currency))

    try:
        with psycopg.connect(load_settings().app_url, connect_timeout=20) as conn:
            with conn.transaction():
                accounts, report = _create_accounts(conn, currencies, args.per_currency)
        for line in report:
            print(line)
        _write_file(accounts, args.output)
        print(f"seeded {len(report)} funding accounts, ids written to {args.output}")
        return 0
    except (SeedException, psycopg.OperationalError, OSError) as e:
        print(f"Seed failed with error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
