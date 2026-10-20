"""check_database (ledger/infra/db.py; docs/design.md, API: /health).

    check_database(pool) -> None

Proves the service can reach the database: it takes one connection from the pool, runs a trivial
statement on it, and gives the connection back. It returns nothing when all is well and lets any
failure through as it is, with one exception: a pool that cannot give a connection in time is a
ServiceBusyError chained to the pool's PoolTimeout, so /health answers 503 like every other route
(a database that cannot be reached still raises psycopg's OperationalError). It is infrastructure:
it runs its own statement, so no service or repository is involved, and /health calls it from the
API layer without touching SQL.

The failure paths that need a broken database are tested with a stand-in pool in
tests/unit/test_check_database_errors.py.
"""

import time

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolTimeout

from ledger.errors import ServiceBusyError
from ledger.infra.db import check_database


@pytest.fixture
def single_pool(settings):
    """A pool of one connection with a short wait: any second connection would have to wait."""
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=0.5, open=True) as pool:
        yield pool


def test_a_healthy_pool_returns_nothing(single_pool: ConnectionPool) -> None:
    assert check_database(single_pool) is None


def test_the_connection_goes_back_to_the_pool(single_pool: ConnectionPool) -> None:
    """With one connection in the pool, repeated checks only work if each one gave it back."""
    for _ in range(5):
        check_database(single_pool)

    with single_pool.connection() as conn:
        assert conn.execute("SELECT 42").fetchone() == (42,)


def test_it_leaves_no_transaction_open(single_pool: ConnectionPool) -> None:
    check_database(single_pool)

    with single_pool.connection() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_it_takes_exactly_one_connection(single_pool: ConnectionPool) -> None:
    """It works on a pool of one, so it never needs a second connection while holding the first."""
    assert check_database(single_pool) is None


def test_a_pool_with_no_free_connection_is_a_service_busy_error_chained_to_the_timeout(
    single_pool: ConnectionPool,
) -> None:
    with single_pool.connection():  # the only connection is in use
        with pytest.raises(ServiceBusyError) as failure:
            check_database(single_pool)

    assert isinstance(failure.value.__cause__, PoolTimeout)


def test_the_timeout_does_not_damage_the_pool(single_pool: ConnectionPool) -> None:
    with single_pool.connection():
        with pytest.raises(ServiceBusyError):
            check_database(single_pool)

    assert check_database(single_pool) is None


def test_a_connection_whose_server_session_died_is_reported_as_a_failure(
    single_pool: ConnectionPool, app_pool: ConnectionPool
) -> None:
    """A check that only looked at the pool would pass with the database session gone. This one
    runs a statement: the pooled connection's backend is terminated from another session, and the
    check fails with the database's own error. The next check succeeds, because the pool replaces
    the dead connection."""
    with single_pool.connection() as conn:
        backend_pid = conn.info.backend_pid
    with app_pool.connection() as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
    time.sleep(0.3)  # the server closes the socket; give it a moment before the next use

    with pytest.raises(psycopg.OperationalError):
        check_database(single_pool)

    assert check_database(single_pool) is None
