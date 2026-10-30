"""Small pieces the concurrency tests share: a transfer as a call for the harness, and two reads."""

from collections.abc import Callable

import psycopg

from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.service.transfer_service import claim_and_post
from tests.db_helpers import hash_for

Call = Callable[[psycopg.Connection], Transfer]


def transfer_call(key: str, payload: TransferPayload) -> Call:
    """A call that claims `key` and posts `payload` through the service, with no retry."""

    def call(conn: psycopg.Connection) -> Transfer:
        return claim_and_post(conn, key, hash_for(key), payload)

    return call


def balance_of(conn: psycopg.Connection, account_id: int) -> int:
    """The cached balance of an account, read through `conn`."""
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def count_entries(conn: psycopg.Connection) -> int:
    """The number of rows in `entries`, read through `conn`."""
    row = conn.execute("SELECT count(*) FROM entries").fetchone()
    assert row is not None
    return row[0]
