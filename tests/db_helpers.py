"""Helpers shared by the tests that use the database."""

import hashlib
import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg_pool import ConnectionPool

from ledger.models.account_model import Account
from ledger.models.posting_model import Entry
from ledger.service.account_service import create_account
from ledger.service.posting_service import claim_transaction, post_transaction

# The database the whole test run uses. docker/roles/roles.sql creates it next to the service's own
# database (`ledger`), because every test run empties its tables.
TEST_DATABASE_NAME = "ledger_test"

# A well-formed request_hash (64 lowercase hex characters), for tests that need any valid value.
VALID_HASH = "a" * 64


def hash_for(label: str) -> str:
    """A well-formed request_hash that differs for every label."""
    return hashlib.sha256(label.encode()).hexdigest()


# Bookkeeping table owned by the migration runner; resetting must not drop its rows.
PRESERVED_TABLES = ("schema_migrations",)

# psycopg waits 130 seconds for a connection when none is set, so a stopped database would make
# the first test that needs it wait over two minutes before failing.
CONNECT_TIMEOUT_SECONDS = 5


def with_database(url: str, name: str) -> str:
    """The URL pointing at another database on the same server, with everything else unchanged."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def with_connect_timeout(url: str, seconds: int = CONNECT_TIMEOUT_SECONDS) -> str:
    """The URL with a connect_timeout in seconds, unless it already sets one."""
    if "connect_timeout=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}connect_timeout={seconds}"


def reset_database(conn: psycopg.Connection) -> None:
    """Empty every application table and restart identity counters.

    Call this from inside a Hypothesis test body: function-scoped fixtures run once per
    test function, not once per generated example.
    """
    rows = conn.execute(
        "SELECT quote_ident(tablename) FROM pg_tables"
        " WHERE schemaname = 'public' AND tablename <> ALL(%s)",
        (list(PRESERVED_TABLES),),
    ).fetchall()
    if rows:
        tables = ", ".join(r[0] for r in rows)
        conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


def make_account(
    conn: psycopg.Connection, name: str, currency: str = "USD", allow_negative: bool = False
) -> Account:
    """create_account with the arguments most tests do not care about defaulted."""
    return create_account(conn, name, currency, allow_negative)


def wait_until_waiting_for_a_lock(
    conn: psycopg.Connection,
    pid: int,
    finished: Callable[[], bool] = lambda: False,
    timeout: float = 5.0,
) -> None:
    """Wait until the backend with this pid has a lock request that is not yet granted.

    Concurrency tests use this to know that a second session is really stuck behind the first
    before they release the first, instead of sleeping and hoping. Pass the other session's
    `connection.info.backend_pid`. `finished` lets the poll give up early when the waiting work has
    already completed (it then never waited). Reads pg_locks, which any role may query, so `conn`
    can be the owner connection.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not finished():
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid = %s", (pid,)
        ).fetchone()
        if row is not None and row[0] > 0:
            return
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited for a lock (it finished, or never started)")


def commit_accounts(pool: ConnectionPool) -> tuple[Account, Account, Account]:
    """Committed USD accounts for tests that read through another connection: a funding account
    (allowed to go negative, the source of money), alice and bob. Remember to reset the database
    afterwards; the pool commits when the block ends."""
    with pool.connection() as conn:
        funding = create_account(conn, "funding:usd", "USD", True)
        alice = create_account(conn, "customer:alice", "USD", False)
        bob = create_account(conn, "customer:bob", "USD", False)
    return funding, alice, bob


def commit_transfer(
    pool: ConnectionPool,
    key: str,
    from_account_id: int,
    to_account_id: int,
    amount_minor: int,
    currency: str = "USD",
    reverses_transaction_id: int | None = None,
) -> int:
    """Claim a key and post a two-entry transfer in its own committed transaction. Returns the
    transaction id."""
    with pool.connection() as conn:
        claim = claim_transaction(conn, key, hash_for(key), reverses_transaction_id)
        post_transaction(
            conn,
            claim.transaction_id,
            [
                Entry(from_account_id, -amount_minor, currency),
                Entry(to_account_id, amount_minor, currency),
            ],
        )
        return claim.transaction_id
