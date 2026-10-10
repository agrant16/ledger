from collections import defaultdict
from dataclasses import dataclass

import psycopg

from ledger.db import require_transaction
from ledger.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    LedgerInvariantError,
    UnbalancedEntriesError,
    UnknownAccountError,
)

INSERT_TRANSACTION_SQL = """
                         INSERT INTO 
                            transactions (idempotency_key, request_hash, reverses_transaction_id) 
                            VALUES (%s, %s, %s) 
                            RETURNING id;
                         """

INSERT_ENTRY_SQL = """
                   INSERT INTO 
                       entries (transaction_id, account_id, amount_minor, currency) 
                       VALUES (%s, %s, %s, %s);
                   """

UPDATE_BALANCE_SQL = """UPDATE 
                            balances 
                            SET balance_minor = balance_minor + %s 
                            WHERE account_id = %s;
                     """


LOCK_ACCOUNTS_SQL = """
                    SELECT 
                        b.account_id, 
                        a.currency, 
                        b.allow_negative, 
                        b.balance_minor 
                    FROM balances b
                    JOIN accounts a 
                    ON a.id = b.account_id
                    WHERE b.account_id = ANY(%s::bigint[])
                    ORDER BY b.account_id ASC
                    FOR UPDATE OF b;"""


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount_minor: int
    currency: str


def claim_transaction(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> int:
    require_transaction(conn, "claim_transaction")

    with conn.cursor() as cursor:
        cursor.execute(
            INSERT_TRANSACTION_SQL, (idempotency_key, request_hash, reverses_transaction_id)
        )
        row = cursor.fetchone()
        return row[0]


def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    require_transaction(conn, "post_transaction")
    if not entries:
        raise LedgerInvariantError(f"No entries for transaction {transaction_id}")

    # check that entries for each account and currency sum to 0
    totals: dict[str, int] = defaultdict(int)
    for entry in entries:
        totals[entry.currency] += entry.amount_minor
    if any(total != 0 for total in totals.values()):
        raise UnbalancedEntriesError(f"Entries for transaction {transaction_id} are not balanced")

    deltas: dict[int, int] = defaultdict(int)
    for entry in entries:
        deltas[entry.account_id] += entry.amount_minor

    # lock accounts in entries and check for unknown accounts
    locked_accounts = _lock_accounts(conn, list(deltas.keys()))
    if len(locked_accounts) != len(deltas.keys()):
        missing = sorted(deltas.keys() - locked_accounts.keys())
        raise UnknownAccountError(
            f"Entries for transaction {transaction_id} include unknown accounts: {missing}"
        )

    # check that accounts in entries have matching currencies with the accounts table.
    for entry in entries:
        if locked_accounts[entry.account_id].currency != entry.currency:
            raise CurrencyMismatchError(
                f"Currency mismatch for account {entry.account_id} on transaction {transaction_id}"
            )

    # check that account has enough balance to cover transactions
    for account_id, delta in sorted(deltas.items()):
        account = locked_accounts[account_id]
        if not account.allow_negative and account.balance_minor + delta < 0:
            raise InsufficientFundsError(account_id, account.balance_minor, delta)

    _insert_entries(conn, transaction_id, entries)
    _apply_balance_deltas(conn, deltas)


def _insert_entries(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    with conn.cursor() as cursor:
        params = [
            (transaction_id, entry.account_id, entry.amount_minor, entry.currency)
            for entry in entries
        ]
        cursor.executemany(INSERT_ENTRY_SQL, params)


def _apply_balance_deltas(conn: psycopg.Connection, deltas: dict[int, int]) -> None:
    with conn.cursor() as cursor:
        for account_id, delta in deltas.items():
            cursor.execute(UPDATE_BALANCE_SQL, (delta, account_id))
            if cursor.rowcount != 1:
                raise LedgerInvariantError(f"No balances row exists for account {account_id}")


@dataclass(frozen=True)
class LockedAccount:
    currency: str
    allow_negative: bool
    balance_minor: int


def _lock_accounts(conn: psycopg.Connection, account_ids: list[int]) -> dict[int, LockedAccount]:
    with conn.cursor() as cursor:
        cursor.execute(LOCK_ACCOUNTS_SQL, (account_ids,))
        rows = cursor.fetchall()
        return {row[0]: LockedAccount(row[1], row[2], row[3]) for row in rows}
