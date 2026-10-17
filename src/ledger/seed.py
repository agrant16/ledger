import argparse
import json
import re
import sys

import psycopg

from ledger.infra.config import load_settings
from ledger.infra.db import create_pool
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
    if not re.match(r"^[A-Z]{3}$", value):
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid format. Must be 3 uppercase letters, e.g. 'USD' or 'EUR'."
        )
    return value


def per_currency_validator(value: str) -> int:
    if not re.match(r"^[1-9]+$", value):
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


def create_accounts(
    conn: psycopg.Connection, currencies: list[str], n: int
) -> dict[str, dict[str, list[int]]] | int:
    accounts = {"funding_accounts": {currency: [] for currency in currencies}}
    for currency in currencies:
        for i in range(0, n):
            account_name = f"funding:{currency}" if i == 0 else f"funding:{currency}:{i + 1}"
            account = find_account_by_name(conn, account_name)
            if account is None:
                account = create_account(conn, account_name, currency, allow_negative=True)
            else:
                if account.currency != currency:
                    raise Exception(
                        f"Account {account_name} already exists but currency does not "
                        f"match {account.currency}"
                    )
                elif not account.allow_negative:
                    raise Exception(
                        f"Account {account_name} already exists but is a protected account"
                    )
            accounts["funding_accounts"][currency].append(account.id)
    return accounts


def write_file(accounts: dict[str, dict[str, list[int]]], path: str) -> None:
    with open(path, "w") as output_file:
        json.dump(accounts, output_file)


def main(argv: list[str] | None = None) -> int:
    parser = set_up_parser()
    args = None
    if argv:
        args = parser.parse_args(argv)
    else:
        args = parser.parse_args()

    pool = create_pool(load_settings())
    try:
        pool.open()
        result = None
        with pool.connection() as conn:
            with conn.transaction():
                result = create_accounts(conn, list(set(args.currency)), args.per_currency)
        if type(result) is int:
            return result
        if result is not None and len(result) > 0:
            print(result)
            write_file(result, args.output)
            return 0
    except Exception as e:
        print(f"Seed failed with error: {e}", file=sys.stderr)
        return 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
