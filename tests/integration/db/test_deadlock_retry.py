"""Deadlock retry in run_in_transaction (ledger/infra/db.py; docs/design.md, Milestone 4; ADR 0017).

    run_in_transaction(pool, work, *, read_only=False) -> whatever work returns

When the database aborts a transaction as a deadlock victim (SQLSTATE 40P01), the whole `work`
function is run again on a fresh transaction, so the idempotency key claim is redone with the
posting (rule 5). These tests raise the deadlock from inside `work`, which is deterministic; the
real thing is in tests/concurrency/test_deadlock_retry.py.

- Up to 3 attempts. A deadlock on the first or second attempt is retried and the later attempt's
  result is returned. Nothing a failed attempt wrote survives.
- Between attempts, and only then, it sleeps a random 10 to 50 ms through `time.sleep`.
- Three deadlocks raise ServiceBusyError, chained to the last DeadlockDetected, and keep nothing.
- The connection is acquired once and every attempt runs on it: the pool is asked for one
  connection however many attempts there are.
- Any other error is not retried: `work` is called once and the error passes as it did before.
  That includes a SerializationFailure (40001), which is a different failure with a similar name.
- A read only run is not retried.
"""

import threading
import time
from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import ConnectionPool

from ledger.errors import DuplicateAccountNameError, LedgerError, ServiceBusyError
from ledger.infra.db import run_in_transaction as run
from ledger.service.account_service import create_account

MAX_ATTEMPTS = 3
MIN_DELAY_SECONDS = 0.010
MAX_DELAY_SECONDS = 0.050


class Boom(Exception):
    pass


class Flaky:
    """A `work` that writes a row, then raises each error in `failures` in turn, then succeeds.

    The row has the same name on every attempt, so an attempt that was not rolled back makes the
    next one fail with a duplicate. It records what each attempt was given.
    """

    def __init__(self, *failures: Callable[[psycopg.Connection], Exception]) -> None:
        self.failures = list(failures)
        self.backend_pids: list[int] = []

    @property
    def calls(self) -> int:
        return len(self.backend_pids)

    def __call__(self, conn: psycopg.Connection) -> str:
        self.backend_pids.append(conn.info.backend_pid)
        create_account(conn, "customer:alice", "USD", False)
        if self.failures:
            raise self.failures.pop(0)(conn)
        return "done"


def deadlock(conn: psycopg.Connection) -> Exception:
    return errors.DeadlockDetected("deadlock detected")


@pytest.fixture
def single_pool(settings):
    """A pool of one connection: any second connection would have to wait for the first."""
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=2, open=True) as pool:
        yield pool


@pytest.fixture
def pair_pool(settings):
    """A pool of two connections, so a second request for a connection is not forced to wait."""
    with ConnectionPool(settings.app_url, min_size=2, max_size=2, timeout=2, open=True) as pool:
        pool.wait(timeout=5)
        yield pool


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


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


# --- retried -------------------------------------------------------------------------------------


def test_a_deadlock_on_the_first_attempt_is_retried_and_the_second_commits(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, sleeps: list[float]
) -> None:
    work = Flaky(deadlock)

    assert run(single_pool, work) == "done"

    assert work.calls == 2
    assert count(owner_conn, "accounts") == 1  # the first attempt's row was rolled back
    assert len(sleeps) == 1


def test_the_third_attempt_can_still_succeed(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, sleeps: list[float]
) -> None:
    work = Flaky(deadlock, deadlock)

    assert run(single_pool, work) == "done"

    assert work.calls == MAX_ATTEMPTS
    assert count(owner_conn, "accounts") == 1
    assert len(sleeps) == MAX_ATTEMPTS - 1


def test_a_run_that_does_not_deadlock_never_sleeps(
    clean_db: None, single_pool, sleeps: list[float]
) -> None:
    work = Flaky()

    assert run(single_pool, work) == "done"

    assert work.calls == 1
    assert sleeps == []


def test_a_run_asks_the_pool_for_one_connection_however_many_attempts(
    clean_db: None, pair_pool, sleeps: list[float]
) -> None:
    """The connection is held across the retries, not given back and requested again: a retry that
    went back to the pool could wait or time out in the middle of a request (ADR 0017). The pool
    has a spare connection, so a re-acquire is visible both in the pool's request count and in the
    backend of a later attempt."""
    work = Flaky(deadlock, deadlock)
    requests_before = pair_pool.get_stats().get("requests_num", 0)

    assert run(pair_pool, work) == "done"

    assert work.calls == MAX_ATTEMPTS
    assert pair_pool.get_stats().get("requests_num", 0) - requests_before == 1
    assert len(set(work.backend_pids)) == 1


def test_a_single_connection_pool_survives_every_attempt(
    clean_db: None, single_pool, sleeps: list[float]
) -> None:
    work = Flaky(deadlock, deadlock)

    run(single_pool, work)

    assert run(single_pool, lambda conn: conn.execute("SELECT 42").fetchone()) == (42,)


# --- exhausted -----------------------------------------------------------------------------------


def test_three_deadlocks_raise_service_busy_chained_to_the_last_deadlock(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, sleeps: list[float]
) -> None:
    last = errors.DeadlockDetected("the last one")
    work = Flaky(deadlock, deadlock, lambda conn: last)

    with pytest.raises(ServiceBusyError) as failure:
        run(single_pool, work)

    assert isinstance(failure.value, LedgerError)
    assert failure.value.__cause__ is last
    assert "deadlock" in str(failure.value).lower()
    assert work.calls == MAX_ATTEMPTS
    assert count(owner_conn, "accounts") == 0


def test_the_connection_is_usable_again_after_the_attempts_run_out(
    clean_db: None, single_pool, sleeps: list[float]
) -> None:
    with pytest.raises(ServiceBusyError):
        run(single_pool, Flaky(deadlock, deadlock, deadlock))

    assert run(single_pool, Flaky()) == "done"


def test_there_is_no_delay_after_the_last_attempt(
    clean_db: None, single_pool, sleeps: list[float]
) -> None:
    with pytest.raises(ServiceBusyError):
        run(single_pool, Flaky(deadlock, deadlock, deadlock))

    assert len(sleeps) == MAX_ATTEMPTS - 1


def test_the_delay_is_random_between_10_and_50_milliseconds(
    clean_db: None, single_pool, sleeps: list[float]
) -> None:
    for _ in range(20):
        with pytest.raises(ServiceBusyError):
            run(single_pool, Flaky(deadlock, deadlock, deadlock))

    assert len(sleeps) == 20 * (MAX_ATTEMPTS - 1)
    assert all(MIN_DELAY_SECONDS <= delay <= MAX_DELAY_SECONDS for delay in sleeps), sleeps
    assert len(set(sleeps)) > 1, "the delay never varies"


# --- not retried ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        pytest.param(lambda conn: Boom(), Boom, id="plain-exception"),
        pytest.param(
            lambda conn: DuplicateAccountNameError("customer:alice"),
            DuplicateAccountNameError,
            id="ledger-error",
        ),
        pytest.param(
            lambda conn: errors.SerializationFailure("could not serialize"),
            errors.SerializationFailure,
            id="serialization-failure-40001",
        ),
        pytest.param(
            lambda conn: errors.LockNotAvailable("lock timeout"),
            errors.LockNotAvailable,
            id="lock-not-available-55P03",
        ),
    ],
)
def test_any_other_error_is_not_retried(
    make_error: Callable[[psycopg.Connection], Exception],
    expected: type[Exception],
    clean_db: None,
    owner_conn: psycopg.Connection,
    single_pool,
    sleeps: list[float],
) -> None:
    """55P03 is listed here only because it is not a deadlock; milestone 4's lock timeout item
    decides what it becomes, and then this case is replaced."""
    work = Flaky(make_error)

    with pytest.raises(expected):
        run(single_pool, work)

    assert work.calls == 1
    assert sleeps == []
    assert count(owner_conn, "accounts") == 0


def test_a_different_error_on_the_retry_propagates(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, sleeps: list[float]
) -> None:
    work = Flaky(deadlock, lambda conn: Boom())

    with pytest.raises(Boom):
        run(single_pool, work)

    assert work.calls == 2
    assert len(sleeps) == 1
    assert count(owner_conn, "accounts") == 0


def test_a_read_only_run_is_not_retried(clean_db: None, single_pool, sleeps: list[float]) -> None:
    calls = []

    def work(conn: psycopg.Connection) -> None:
        calls.append(True)
        raise errors.DeadlockDetected("deadlock detected")

    with pytest.raises(errors.DeadlockDetected):
        run(single_pool, work, read_only=True)

    assert len(calls) == 1
    assert sleeps == []
