import argparse
import json
import os
import re
import sys
import tempfile

import psycopg

from ledger.errors import CurrencyMismatchError, DuplicateAccountNameError, LedgerInvariantError
from ledger.infra.config import load_settings
from ledger.models.account_model import Account
from ledger.repository.account_repo import find_account_by_name
from ledger.service.account_service import create_account


class OverrideAppendAction(argparse._AppendAction):
    def __call__(self, parser, namespace, values, option_string=None):
        # Check if the current value in the namespace is still the default list
        if getattr(namespace, self.dest) == self.default:
            # Clear it out so we don't append to the default
            setattr(namespace, self.dest, [])
        # Call the standard append logic
        super().__call__(parser, namespace, values, option_string)


def currency_validator(value: str) -> str:
    # fullmatch, not match with ^...$: `$` also matches before a trailing newline.
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid format. Must be 3 uppercase letters, e.g. 'USD' or 'EUR'."
        )
    return value


def per_currency_validator(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--per-currency must be positive integer.")
    return int(value)


def set_up_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the database with funding accounts", prog="python -m ledger.seed"
    )
    parser.add_argument(
        "--currency",
        action=OverrideAppendAction,
        type=currency_validator,
        default=["USD", "EUR"],
        help="Currency code to include (e.g., --currency USD --currency EUR). Can be repeated.",
    )
    parser.add_argument(
        "--per-currency",
        type=per_currency_validator,
        default=1,
        help="Number of records (N) to generate per currency.",
    )

    # --output is 100% required
    parser.add_argument(
        "-o", "--output", required=True, help="PATH where the output ledger file will be saved."
    )
    return parser


def ensure_funding_account(
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
        raise CurrencyMismatchError(
            f"Account {account_name} already exists but currency does not match {account.currency}"
        )
    if not account.allow_negative:
        raise ValueError(f"Account {account_name} already exists but is a protected account")
    return account, created


def create_accounts(
    conn: psycopg.Connection, currencies: list[str], n: int
) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
    """The ids file content and one report line per account."""
    accounts = {"funding_accounts": {currency: [] for currency in currencies}}
    report: list[str] = []
    for currency in currencies:
        for i in range(0, n):
            account_name = f"funding:{currency}" if i == 0 else f"funding:{currency}:{i + 1}"
            account, created = ensure_funding_account(conn, account_name, currency)
            accounts["funding_accounts"][currency].append(account.id)
            report.append(f"{'created' if created else 'exists '} {account_name} (id {account.id})")
    return accounts, report


def write_file(accounts: dict[str, dict[str, list[int]]], target_path: str) -> None:
    target_dir = os.path.dirname(os.path.abspath(target_path))
    tmp_file = tempfile.NamedTemporaryFile(dir=target_dir, delete=False, mode="w", encoding="utf-8")

    try:
        with tmp_file:
            json.dump(accounts, tmp_file)
        os.replace(tmp_file.name, target_path)
    except OSError as e:
        # 5. Clean up the temp file if anything fails before the replace happens
        if os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)
        raise e


def main(argv: list[str] | None = None) -> int:
    args = set_up_parser().parse_args(argv)  # None reads sys.argv; a usage error exits with 2
    # dict.fromkeys drops repeated currencies and keeps the order they were given in.
    currencies = list(dict.fromkeys(args.currency))

    try:
        with psycopg.connect(load_settings().app_url) as conn:
            with conn.transaction():
                accounts, report = create_accounts(conn, currencies, args.per_currency)
        for line in report:
            print(line)
        write_file(accounts, args.output)
        print(f"seeded {len(report)} funding accounts, ids written to {args.output}")
        return 0
    except RuntimeError as e:
        print(f"Seed failed with error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
