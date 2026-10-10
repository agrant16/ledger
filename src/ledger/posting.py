from dataclasses import dataclass

import psycopg

from ledger.db import require_transaction
from ledger.errors import LedgerInvariantError

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
    pass


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
    pass
