from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount_minor: int
    currency: str


def claim_transaction(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None,
) -> int:
    pass


def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    pass


def _insert_entries(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    pass


def _apply_balance_deltas(conn: psycopg.Connection, deltas: dict[int, int]) -> None:
    pass
