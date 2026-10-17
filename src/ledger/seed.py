import argparse
import json
import sys
from collections import defaultdict

import psycopg

from ledger.errors import LedgerInvariantError
from ledger.infra.db import create_pool
from ledger.infra.config import load_settings
from ledger.repository.account_repo import find_account_by_name
from ledger.service.account_service import create_account


def set_up_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the database with funding accounts", prog="python -m ledger.seed"
    )
    parser.add_argument(
        "--currency",
        action="append",
        default = ["USD"],
        help="Currency code to include (e.g., --currency USD --currency EUR). Can be repeated.",
    )
    parser.add_argument("--per-currency", type=int, default= 1, help="Number of records (N) to generate per currency.")

    # --output is 100% required
    parser.add_argument("-o", "--output", required=True, help="PATH where the output ledger file will be saved.")
    return parser


def create_accounts(conn: psycopg.Connection, currencies: list[str], n: int) -> dict[str, dict[str, list[int]]]:
    accounts = {"funding_accounts": {currency: [] for currency in currencies}}
    for currency in currencies:
        for i in range(0, n):
            account_name = f"funding:{currency}" if i == 0 else f"funding:{currency}:{i+1}"
            account = find_account_by_name(conn, account_name)
            if account is None:
                account = create_account(conn, account_name, currency, allow_negative=True)
            else:
                if account.currency != currency:
                    print(f"Account {account_name} already exists but currency does not match {account.currency} != {currency}")
                    sys.exit(1)
                elif not account.allow_negative:
                    print(f"Account {account_name} already exists but is a protected account, allow_negative=False")
                    sys.exit(1)
            accounts["funding_accounts"][currency].append(account.id)
    return accounts

def write_file(accounts: dict[str, dict[str, list[int]]], path: str) -> None:
    with open(path, "w") as output_file:
        json.dump(accounts, output_file)

def main() -> None:
    parser = set_up_parser()
    args = parser.parse_args()
    if args.per_currency < 1:
        print("--per-currency must be >= 1")
        sys.exit(2)

    pool = create_pool(load_settings())
    try:
        pool.open()
        accounts = None
        with pool.connection() as conn:
            with conn.transaction():
               accounts = create_accounts(conn, list(set(args.currency)), args.per_currency)
        print(len(accounts))
        if accounts is not None and len(accounts) > 0:
            print(accounts)
            write_file(accounts, args.output)
    except Exception as e:
        print(f"Seed failed with error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        pool.close()
if __name__ == "__main__":
    main()
