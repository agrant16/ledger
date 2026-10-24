"""Seeds the database with funding accounts.

Usage:
    python -m ledger.seed --output PATH [--currency CODE ...] [--per-currency N]

Creates `funding:<CURRENCY>` accounts (further ones `:2`, `:3`, ...) with `allow_negative` true
and writes their ids to the `--output` file as JSON. It is safe to run again, and to run twice at
once: existing accounts are reused.

Exit codes:
    0: Success.
    1: Failure. No file is written. Nothing is committed unless the failure was writing the file.
    2: Usage error.
"""

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

DEFAULT_CURRENCIES = ("USD", "EUR")
ACCOUNT_PER_CURRENCY_MAX = 500


class SeedError(Exception):
    """Raised when an existing account does not match what the seed would have created."""


def _currency_validator(value: str) -> str:
    """Validates that the value for `--currency` is three uppercase letters."""
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid format. Must be 3 uppercase letters, e.g. 'USD' or 'EUR'."
        )
    return value


def _per_currency_validator(value: str) -> int:
    """Validates the --per-currency value is an integer between 1 and 500."""
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--per-currency must be a positive integer.")
    integer = int(value)
    if integer > ACCOUNT_PER_CURRENCY_MAX:
        raise argparse.ArgumentTypeError(
            f"--per-currency must be a positive integer <= {ACCOUNT_PER_CURRENCY_MAX}."
        )
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
        f"Can be repeated. Defaults to {', '.join(DEFAULT_CURRENCIES)}.",
    )
    parser.add_argument(
        "--per-currency",
        type=_per_currency_validator,
        default=1,
        help="Number of records (N) to generate per currency. "
        f"Must be 1 <= N <= {ACCOUNT_PER_CURRENCY_MAX}",
    )

    # --output is 100% required
    parser.add_argument(
        "-o", "--output", required=True, help="PATH where the output ledger file will be saved."
    )
    return parser


def _create_or_find(
    conn: psycopg.Connection, account_name: str, currency: str
) -> tuple[Account, bool]:
    """Creates the funding account, or returns the one another run committed first.

    Args:
        conn: An open connection, inside the caller's transaction.
        account_name: The name of the account.
        currency: The currency of the account.

    Returns:
        The account, and whether this call created it.

    Raises:
        LedgerInvariantError: The name was a duplicate but no account has it.
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
    """Returns the funding account with this name, creating it if it does not exist.

    An existing account is accepted only if it has the given currency and `allow_negative` true.

    Args:
        conn: An open connection, inside the caller's transaction.
        account_name: The name of the account.
        currency: The currency the account must have.

    Returns:
        The account, and whether this call created it.

    Raises:
        SeedError: The account exists with another currency, or with `allow_negative` false.
        LedgerInvariantError: See `_create_or_find`.
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
    """Creates or finds the funding accounts of every currency.

    The accounts are created in sorted currency order, so two runs that name the same currencies
    in opposite orders cannot deadlock.

    Args:
        conn: An open connection, inside the caller's transaction.
        currencies: The currencies, without repeats.
        n: The number of accounts per currency.

    Returns:
        The content of the ids file, with the currencies in the order given and the ids in index
        order, and one report line per account.

    Raises:
        SeedError: See `_ensure_funding_account`.
        LedgerInvariantError: See `_ensure_funding_account`.
    """
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
    """Writes the accounts as JSON to the target path, replacing any file there.

    The content goes to a temporary file in the same directory and is then renamed, so the target
    holds either its old content or the complete new content.

    Args:
        accounts: The content of the ids file.
        target_path: The path of the file to write.

    Raises:
        OSError: The file cannot be written. The temporary file is removed.
    """
    target = Path(target_path)

    with tempfile.NamedTemporaryFile(
        dir=target.resolve().parent, delete=False, mode="w", encoding="utf-8"
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
        try:
            json.dump(accounts, tmp_file)
            # Flush data to disk before closing/renaming
            tmp_file.flush()
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise

    # The file is guaranteed closed here, making the rename safe across all OS platforms
    try:
        tmp_path.replace(target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Runs the seed script.

    All accounts are created in one database transaction. The ids file is written only after
    that transaction has committed, so a failed run writes no file.

    Args:
        argv: The command-line arguments. `None` reads `sys.argv`.

    Returns:
        The exit code: 0 on success, 1 when the seed fails because an existing account does not
        match, the database cannot be reached, or the file cannot be written.

    Raises:
        SystemExit: The arguments are invalid. The exit code is 2.
        LedgerInvariantError: See `_ensure_funding_account`.
    """
    args = _set_up_parser().parse_args(argv)  # None reads sys.argv; a usage error exits with 2
    # dict.fromkeys drops repeated currencies and keeps the order they were given in.
    currencies = list(dict.fromkeys(args.currency or DEFAULT_CURRENCIES))

    try:
        with (
            psycopg.connect(load_settings().app_url, connect_timeout=20) as conn,
            conn.transaction(),
        ):
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
