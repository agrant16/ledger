from dataclasses import dataclass

import psycopg
from psycopg import errors

from ledger.db import require_transaction
from ledger.errors import (
    LedgerInvariantError,
    UnbalancedEntriesError,
    InsufficientFundsError,
    UnknownAccountError,
    CurrencyMismatchError,
)


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount_minor: int
    currency: str


def claim_transaction(
    conn: psycopg.Connection, idempotency_key, request_hash, reverses_transaction_id=None
) -> int:
    pass


def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    pass


def _insert_entries(conn: psycopg.Connection, entries: list[Entry]) -> None:
    pass


def _apply_balance_deltas(conn: psycopg.Connection) -> None:
    pass
