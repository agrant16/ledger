from dataclasses import dataclass

import psycopg
from ledger.db import require_transaction

INSERT_TRANSACTION_SQL = """
                         INSERT INTO transactions (idempotency_key, request_hash, reverses_transaction_id) VALUES (%s, %s, %s) RETURNING id;
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
    cursor = conn.cursor()
    cursor.execute(INSERT_TRANSACTION_SQL, (idempotency_key, request_hash, reverses_transaction_id))
    row = cursor.fetchone()
    return row[0]


def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    pass


def _insert_entries(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    pass


def _apply_balance_deltas(conn: psycopg.Connection, deltas: dict[int, int]) -> None:
    pass

@dataclass(frozen=True)
class LockedAccount:
    account_id: int
    account_name: str
    currency: str
    allow_negative: bool

def _lock_accounts(conn: psycopg.Connection, account_id: list[int]):
    pass
