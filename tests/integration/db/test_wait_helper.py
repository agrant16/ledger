"""wait_until_waiting_for_a_lock (tests/db_helpers.py): how the concurrency tests know that a second
session is really stuck behind the first.

The helper has to be exact about *which* session it is asking about. A version that looked for any
waiting lock request in the database would let a test pass when some other session happened to be
waiting, which is how an unrelated stray lock could hide a broken test.
"""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.accounts import create_account
from tests.db_helpers import wait_until_waiting_for_a_lock

LOCK_THE_ROW = "SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE"


@pytest.fixture
def account_id(clean_db: None, app_conn: psycopg.Connection, commit_setup) -> int:
    account = create_account(app_conn, "customer:alice", "USD", False)
    commit_setup()
    return account.id


def test_it_returns_once_the_session_is_waiting_for_a_lock(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool, account_id: int
) -> None:
    with (
        app_pool.connection() as holder,
        app_pool.connection() as waiter,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        try:
            holder.execute(LOCK_THE_ROW, (account_id,))
            future = executor.submit(waiter.execute, LOCK_THE_ROW, (account_id,))

            wait_until_waiting_for_a_lock(owner_conn, waiter.info.backend_pid, future.done)

            assert not future.done()
        finally:
            holder.rollback()
        future.result(timeout=5)
        waiter.rollback()


def test_it_fails_when_the_session_is_not_waiting(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool
) -> None:
    with app_pool.connection() as idle:
        with pytest.raises(AssertionError, match="never waited"):
            wait_until_waiting_for_a_lock(owner_conn, idle.info.backend_pid, timeout=0.2)


def test_it_stops_at_once_when_the_work_has_already_finished(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool
) -> None:
    with app_pool.connection() as idle:
        with pytest.raises(AssertionError, match="never waited"):
            wait_until_waiting_for_a_lock(
                owner_conn, idle.info.backend_pid, finished=lambda: True, timeout=30
            )


def test_another_sessions_wait_does_not_count(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool, account_id: int
) -> None:
    """One session is stuck on a lock; the session we ask about is idle. The answer is no."""
    with (
        app_pool.connection() as holder,
        app_pool.connection() as stuck,
        app_pool.connection() as idle,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        try:
            holder.execute(LOCK_THE_ROW, (account_id,))
            future = executor.submit(stuck.execute, LOCK_THE_ROW, (account_id,))
            wait_until_waiting_for_a_lock(owner_conn, stuck.info.backend_pid, future.done)

            with pytest.raises(AssertionError, match="never waited"):
                wait_until_waiting_for_a_lock(owner_conn, idle.info.backend_pid, timeout=0.2)
        finally:
            holder.rollback()
        future.result(timeout=5)
        stuck.rollback()
