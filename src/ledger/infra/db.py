from collections.abc import Callable
from contextlib import ExitStack

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ledger.errors import LedgerInvariantError, ServiceBusyError
from ledger.infra.config import Settings


def create_pool(settings: Settings) -> ConnectionPool:
    """Fixed-size pool of app-role connections (min_size == max_size, so it never grows).

    A request must use one connection for its whole transaction and never take a second
    while holding the first: with every connection held by a request waiting for another,
    the pool would deadlock.
    """
    return ConnectionPool(
        settings.app_url,
        min_size=settings.pool_size,
        max_size=settings.pool_size,
        timeout=settings.pool_timeout_seconds,
        open=False,
    )


def _acquire(pool: ConnectionPool, stack: ExitStack) -> psycopg.Connection:
    """Acquire a connection to the ledger db and protect against PoolTimeout."""
    try:
        return stack.enter_context(pool.connection())
    except PoolTimeout as err:
        raise ServiceBusyError("Connection timeout") from err


def run_in_transaction[T](
    pool: ConnectionPool,
    work: Callable[[psycopg.Connection], T],
    *,
    read_only: bool = False,
) -> T:
    """Run `work` on one pooled connection as a single database transaction.

        The connection is committed when `work` returns and rolled back if it raises, so
        service functions never commit (the caller owns the transaction). `work` gets the
        one connection for the whole request and must not take a second from the pool.
        A retry should wrap the whole call, so the idempotency key claim and the posting
        are retried together.

        With `read_only`, the transaction is set READ ONLY before `work` runs. The
        isolation level is the default, READ COMMITTED.

        Returns whatever `work` returns.

        Raises:
            ServiceBusyError: no connection became free within the pool timeout.
            LedgerInvariantError: the database rejected the write with an integrity
                error (a constraint, such as a CHECK or foreign key, was violated). This
                is a bug that the database safety net caught, not an expected failure,
                so it is not a `LedgerError` and the API returns a logged 500.

        Any other exception from `work`, including `LedgerError` subclasses, rolls the
        transaction back and propagates unchanged.
    """
    try:
        with ExitStack() as stack:
            conn = _acquire(pool, stack)
            if read_only:
                conn.execute("SET TRANSACTION READ ONLY")
            return work(conn)
    except psycopg.errors.IntegrityError as failure:
        raise LedgerInvariantError(
            f"A database constraint was violated: ({failure.diag.constraint_name})"
        ) from failure


def check_database(pool: ConnectionPool) -> None:
    """Raise if the database cannot answer; used by GET /health."""
    with ExitStack() as stack:
        conn = _acquire(pool, stack)
        conn.execute("SELECT 1")
