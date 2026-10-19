"""run_in_transaction (ledger/infra/db.py; docs/design.md, Stack decisions; ADRs 0011 and 0013).

    run_in_transaction(pool, work, *, read_only=False) -> whatever work returns

A request takes one connection from the pool, runs all of its database work on it as one database
transaction, and commits inside the handler, so an error raised by the commit itself (the deferred
constraint triggers fire there) is still seen by the handler and becomes a logged 500. `work` is a
function of the connection that does everything and returns plain data, which is what lets
milestone 4's deadlock retry run the whole body again without touching any route.

- The connection is not in autocommit mode (the service functions refuse that).
- On success it commits and returns work's result.
- If work raises, it rolls back and the same exception propagates.
- If the commit raises, nothing is kept and the error propagates. A violated database constraint
  (the deferred triggers fire at commit) is a bug, so it becomes a LedgerInvariantError that is not
  a LedgerError, chained to the original psycopg error. The same translation covers an integrity
  error raised inside `work` that nothing below translated. A LedgerError raised by `work` is an
  expected outcome and passes through untouched, and so does any other database error.
- A pool that cannot give a connection within its wait is a ServiceBusyError (an expected outcome
  that the API layer maps to a 503), chained to the pool's PoolTimeout. Only the wait for the
  connection counts: `work` is not called, and a PoolTimeout raised from inside `work` is not
  rewritten. The pool is fine again once a connection is free.
- Either way the connection goes back to the pool in a usable state.
- With read_only=True the transaction is read only (SET TRANSACTION READ ONLY, issued first), and
  that setting does not leak onto the next user of the pooled connection.
"""

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from ledger.errors import (
    DuplicateAccountNameError,
    LedgerError,
    LedgerInvariantError,
    ServiceBusyError,
)
from ledger.service.account_service import create_account
from tests.db_helpers import VALID_HASH

LONE_TRANSACTION = "INSERT INTO transactions (idempotency_key, request_hash) VALUES ('k', %s)"


class Boom(Exception):
    pass


@pytest.fixture
def run():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.infra.db import run_in_transaction

    return run_in_transaction


@pytest.fixture
def single_pool(settings):
    """A pool of one connection: any second connection would have to wait for the first."""
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=2, open=True) as pool:
        yield pool


@pytest.fixture
def busy_pool(settings):
    """A pool of one connection that gives up after 0.3 seconds, so a test can exhaust it."""
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=0.3, open=True) as pool:
        yield pool


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


def test_the_work_gets_a_connection_with_a_transaction_in_progress(
    clean_db: None, single_pool, run
) -> None:
    assert run(single_pool, lambda conn: conn.autocommit) is False


def test_a_successful_run_commits_and_returns_the_result(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    account = run(single_pool, lambda conn: create_account(conn, "customer:alice", "USD", False))

    assert account.account_name == "customer:alice"
    assert (count(owner_conn, "accounts"), count(owner_conn, "balances")) == (1, 1)


def test_a_run_whose_work_raises_rolls_back_and_re_raises(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    def work(conn):
        create_account(conn, "customer:alice", "USD", False)
        raise Boom

    with pytest.raises(Boom):
        run(single_pool, work)

    assert (count(owner_conn, "accounts"), count(owner_conn, "balances")) == (0, 0)


def test_a_failed_commit_raises_and_keeps_nothing(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    """A transaction row with no entries is refused by the deferred trigger at commit. That is a
    bug, so the caller gets a LedgerInvariantError (never a LedgerError) and the psycopg error
    is kept as its cause."""
    with pytest.raises(LedgerInvariantError) as failure:
        run(single_pool, lambda conn: conn.execute(LONE_TRANSACTION, (VALID_HASH,)))

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)
    assert count(owner_conn, "transactions") == 0


def test_the_connection_is_usable_again_after_each_kind_of_failure(
    clean_db: None, single_pool, run
) -> None:
    """With one connection in the pool, a later run only works if the failed run gave its
    connection back, and in a state where it can start a new transaction."""

    def raises(conn):
        raise Boom

    with pytest.raises(Boom):
        run(single_pool, raises)
    assert run(single_pool, lambda conn: conn.execute("SELECT 42").fetchone()) == (42,)

    with pytest.raises(LedgerInvariantError):
        run(single_pool, lambda conn: conn.execute(LONE_TRANSACTION, (VALID_HASH,)))
    assert run(single_pool, lambda conn: conn.execute("SELECT 43").fetchone()) == (43,)


def test_many_failures_do_not_leak_connections(clean_db: None, settings, run) -> None:
    with ConnectionPool(settings.app_url, min_size=2, max_size=2, timeout=2, open=True) as pool:

        def raises(conn):
            raise Boom

        for _ in range(6):
            with pytest.raises(Boom):
                run(pool, raises)
        assert run(pool, lambda conn: conn.execute("SELECT 1").fetchone()) == (1,)


def test_a_run_takes_exactly_one_connection(clean_db: None, single_pool, run) -> None:
    """It works on a pool of one, so it never needs a second connection while holding the first,
    which is what would deadlock a full pool."""
    assert run(single_pool, lambda conn: conn.execute("SELECT 1").fetchone()) == (1,)


def test_a_read_only_run_can_read(clean_db: None, single_pool, run) -> None:
    result = run(single_pool, lambda conn: conn.execute("SELECT 1").fetchone(), read_only=True)
    assert result == (1,)


def test_a_read_only_run_cannot_write(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        run(
            single_pool,
            lambda conn: create_account(conn, "customer:alice", "USD", False),
            read_only=True,
        )

    assert count(owner_conn, "accounts") == 0


def test_the_read_only_setting_does_not_reach_the_next_run(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    """The pool has one connection, so the next run reuses the very one that was read only."""
    run(single_pool, lambda conn: conn.execute("SELECT 1").fetchone(), read_only=True)

    run(single_pool, lambda conn: create_account(conn, "customer:alice", "USD", False))

    assert count(owner_conn, "accounts") == 1


# --- database errors and the error taxonomy (ADR 0013) ----------------------------------------


def test_an_integrity_error_inside_the_work_becomes_a_chained_invariant_error(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    """Nothing below translated this duplicate, so it is a bug: the caller never sees a raw
    psycopg integrity error, and the original is kept as the cause."""

    def work(conn):
        conn.execute(
            "INSERT INTO accounts (account_name, currency, allow_negative)"
            " VALUES ('customer:alice', 'USD', false)"
        )
        conn.execute(
            "INSERT INTO accounts (account_name, currency, allow_negative)"
            " VALUES ('customer:alice', 'USD', false)"
        )

    with pytest.raises(LedgerInvariantError) as failure:
        run(single_pool, work)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)
    assert count(owner_conn, "accounts") == 0


def test_a_check_violation_inside_the_work_becomes_a_chained_invariant_error(
    clean_db: None, single_pool, run
) -> None:
    def work(conn):
        conn.execute(
            "INSERT INTO accounts (account_name, currency, allow_negative)"
            " VALUES ('customer:alice', 'usd', false)"
        )

    with pytest.raises(LedgerInvariantError) as failure:
        run(single_pool, work)

    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)


def test_an_expected_ledger_error_passes_through_untouched(
    clean_db: None, owner_conn: psycopg.Connection, single_pool, run
) -> None:
    expected = DuplicateAccountNameError("customer:alice")

    def work(conn):
        create_account(conn, "customer:bob", "USD", False)
        raise expected

    with pytest.raises(DuplicateAccountNameError) as failure:
        run(single_pool, work)

    assert failure.value is expected
    assert count(owner_conn, "accounts") == 0


def test_a_database_error_that_is_not_an_integrity_error_is_not_translated(
    clean_db: None, single_pool, run
) -> None:
    """Only constraint violations are translated. A syntax error is a different kind of failure
    and keeps its own type."""
    with pytest.raises(psycopg.errors.SyntaxError):
        run(single_pool, lambda conn: conn.execute("SELEKT 1"))


def test_a_pool_wait_timeout_is_a_service_busy_error_after_the_wait(busy_pool, run) -> None:
    with busy_pool.connection():  # the only connection is in use
        started = time.monotonic()
        with pytest.raises(ServiceBusyError) as failure:
            run(busy_pool, lambda conn: conn.execute("SELECT 1"))
        waited = time.monotonic() - started

    assert isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, PoolTimeout)
    assert 0.25 <= waited < 5  # it waited the pool's 0.3 seconds, and did not hang


def test_the_work_is_not_called_when_no_connection_is_free(busy_pool, run) -> None:
    called = []

    with busy_pool.connection():
        with pytest.raises(ServiceBusyError):
            run(busy_pool, lambda conn: called.append(True))

    assert called == []


def test_a_pool_timeout_raised_inside_the_work_is_not_rewritten(busy_pool, run) -> None:
    """Only the wait for the connection is a busy service. The work already holds its connection,
    so a PoolTimeout coming out of it would be a bug (the one-connection rule) and must stay one."""

    def work(conn):
        raise PoolTimeout("a second connection was requested")

    with pytest.raises(PoolTimeout):
        run(busy_pool, work)


def test_the_pool_works_again_once_a_connection_is_free(busy_pool, run) -> None:
    with busy_pool.connection():
        with pytest.raises(ServiceBusyError):
            run(busy_pool, lambda conn: conn.execute("SELECT 1"))

    assert run(busy_pool, lambda conn: conn.execute("SELECT 42").fetchone()) == (42,)
