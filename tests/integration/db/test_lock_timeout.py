"""Lock timeout in run_in_transaction (ledger/infra/db.py; docs/design.md, Milestone 4; ADR 0017).

    run_in_transaction(pool, work, *, read_only=False) -> whatever work returns

Every write transaction starts with `lock_timeout` set to 2 seconds, so a request stuck behind
another transaction's lock fails instead of hanging. The wait that fails is a ServiceBusyError
(ADR 0016): the transaction rolled back and nothing was written, so the client can retry. It is not
retried inside, because the holder that blocked the first attempt would block the next one too.

- A write run has the setting; a read only run does not (it takes no row locks to wait on).
- The setting lasts one transaction. A pooled connection that has run a write is back to no
  timeout after a commit, a rollback and a timeout.
- A row lock held by another session makes the run fail with ServiceBusyError after about the
  timeout. The cause is the database's LockNotAvailable (55P03), the message names the lock
  timeout, `work` ran once, nothing slept (no retry), what `work` wrote before the wait is rolled
  back, and the connection is usable again.

The real wait is made short by patching `ledger.infra.db.LOCK_TIMEOUT_MS`; the default is asserted
as it is. Each held lock has a safety release, so if the timeout is missing the run is held only
for a few seconds and the test fails on its assertions, instead of waiting for pytest-timeout to
kill the whole run.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import ConnectionPool

from ledger.errors import LedgerError, ServiceBusyError
from ledger.infra.config import Settings
from ledger.infra.db import run_in_transaction as run
from ledger.repository.posting_repo import lock_accounts
from ledger.service.account_service import create_account
from tests.db_helpers import commit_accounts

# Not imported from ledger.infra.db because the tests here are for the contract and not the value.
DEFAULT_LOCK_TIMEOUT = "2s"  # what `SHOW lock_timeout` prints for 2000 ms
SHORT_LOCK_TIMEOUT_MS = 300
SAFETY_RELEASE_SECONDS = 3  # well above the short timeout, well below the test timeout


class Boom(Exception):
    pass


@pytest.fixture
def single_pool(settings):
    """A pool of one connection: every run reuses the very connection the last one used."""
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=2, open=True) as pool:
        yield pool


@pytest.fixture
def short_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ledger.infra.db.LOCK_TIMEOUT_MS", SHORT_LOCK_TIMEOUT_MS)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records the delays the test thread asks `time.sleep` for, without sleeping.

    Other threads (the pool's workers) keep the real `time.sleep`.
    """
    recorded: list[float] = []
    real_sleep = time.sleep
    test_thread = threading.current_thread()

    def fake_sleep(seconds: float) -> None:
        if threading.current_thread() is test_thread:
            recorded.append(seconds)
        else:
            real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return recorded


@contextmanager
def holding_row_lock(settings: Settings, account_id: int) -> Iterator[None]:
    """Hold `FOR UPDATE` on an account's balances row from a session of the test's own.

    The lock is released when the block ends, or after SAFETY_RELEASE_SECONDS, whichever is first.
    """
    with psycopg.connect(settings.owner_url) as holder:
        holder.execute("SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE", (account_id,))
        release = threading.Timer(SAFETY_RELEASE_SECONDS, holder.rollback)
        release.start()
        try:
            yield
        finally:
            release.cancel()
            holder.rollback()


def show_lock_timeout(conn: psycopg.Connection) -> str:
    row = conn.execute("SHOW lock_timeout").fetchone()
    assert row is not None
    return row[0]


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return row[0]


# --- the setting ---------------------------------------------------------------------------


def test_a_write_run_has_a_two_second_lock_timeout(clean_db: None, single_pool) -> None:
    assert run(single_pool, show_lock_timeout) == DEFAULT_LOCK_TIMEOUT


def test_a_read_only_run_has_no_lock_timeout(clean_db: None, single_pool) -> None:
    assert run(single_pool, show_lock_timeout, read_only=True) == "0"


@pytest.mark.parametrize("fail", [False, True], ids=["commit", "rollback"])
def test_the_setting_does_not_reach_the_next_user_of_the_connection(
    fail: bool, clean_db: None, single_pool
) -> None:
    """The pool has one connection, so what follows reuses the one that ran the write."""

    def work(conn: psycopg.Connection) -> None:
        assert show_lock_timeout(conn) == DEFAULT_LOCK_TIMEOUT
        if fail:
            raise Boom

    if fail:
        with pytest.raises(Boom):
            run(single_pool, work)
    else:
        run(single_pool, work)

    with single_pool.connection() as conn:
        assert show_lock_timeout(conn) == "0"


# --- the wait times out ------------------------------------------------------------------------


def test_a_held_row_lock_makes_the_run_fail_with_service_busy_after_the_timeout(
    clean_db: None,
    settings,
    owner_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    single_pool,
    short_lock_timeout: None,
    sleeps: list[float],
) -> None:
    _, alice, _ = commit_accounts(app_pool)
    accounts_before = count(owner_conn, "accounts")
    calls: list[bool] = []

    def work(conn: psycopg.Connection) -> None:
        calls.append(True)
        create_account(conn, "customer:early", "USD", False)  # written before the wait
        lock_accounts(conn, [alice.id])  # the real locking statement; this is where it waits

    with holding_row_lock(settings, alice.id):
        started = time.monotonic()
        with pytest.raises(ServiceBusyError) as failure:
            run(single_pool, work)
        waited = time.monotonic() - started

    assert isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, errors.LockNotAvailable)
    assert "lock timeout" in str(failure.value).lower()
    assert 0.25 <= waited < SAFETY_RELEASE_SECONDS
    assert len(calls) == 1
    assert sleeps == []
    assert count(owner_conn, "accounts") == accounts_before


def test_the_connection_is_usable_and_has_no_timeout_after_a_lock_timeout(
    clean_db: None,
    settings,
    app_pool: ConnectionPool,
    single_pool,
    short_lock_timeout: None,
) -> None:
    _, alice, _ = commit_accounts(app_pool)

    with holding_row_lock(settings, alice.id):
        with pytest.raises(ServiceBusyError):
            run(single_pool, lambda conn: lock_accounts(conn, [alice.id]))

    # The holder is gone, so the same locking statement now succeeds on the very same connection.
    locked = run(single_pool, lambda conn: lock_accounts(conn, [alice.id]))
    assert list(locked) == [alice.id]
    with single_pool.connection() as conn:
        assert show_lock_timeout(conn) == "0"


def test_a_lock_not_available_error_is_service_busy_and_attempted_once(
    clean_db: None,
    owner_conn: psycopg.Connection,
    single_pool,
    sleeps: list[float],
) -> None:
    """The error itself, raised from inside `work`, so the mapping is shown without a real wait."""
    raised = errors.LockNotAvailable("canceling statement due to lock timeout")
    calls: list[bool] = []

    def work(conn: psycopg.Connection) -> None:
        calls.append(True)
        create_account(conn, "customer:alice", "USD", False)
        raise raised

    with pytest.raises(ServiceBusyError) as failure:
        run(single_pool, work)

    assert failure.value.__cause__ is raised
    assert "lock timeout" in str(failure.value).lower()
    assert len(calls) == 1
    assert sleeps == []
    assert count(owner_conn, "accounts") == 0
