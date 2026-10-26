from collections.abc import Callable
from contextlib import ExitStack

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ledger.errors import LedgerInvariantError, ServiceBusyError
from ledger.infra.config import Settings


def create_pool(settings: Settings) -> ConnectionPool:
    """Creates a fixed-size pool of app-role connections that never grows.

    A request must use one connection for its whole transaction and never take a second
    while holding the first.

    Args:
        settings: The service settings (app-role URL, pool size and wait timeout).

    Returns:
        A pool that is not yet open. A caller waits up to the configured timeout for a free
        connection.
    """
    return ConnectionPool(
        settings.app_url,
        min_size=settings.pool_size,
        max_size=settings.pool_size,
        timeout=settings.pool_timeout_seconds,
        open=False,
    )


def _acquire(pool: ConnectionPool, stack: ExitStack) -> psycopg.Connection:
    """Acquires a connection from the pool, raising `ServiceBusyError` on a wait timeout."""
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
    """Runs `work` on one pooled connection as a single database transaction.

    This is where a request's transaction starts and ends. Service functions take the
    connection and never commit (the caller owns the transaction), so everything `work`
    does either commits together when it returns or rolls back together when it raises.

    `work` receives the one connection for the whole request. It must not commit or roll
    back, and it must not take a second connection from the pool while holding this one.
    A retry should wrap the whole call, not part of `work`, so the idempotency key claim
    and the posting are retried together on a fresh transaction.

    The isolation level is the default, READ COMMITTED. With `read_only`, the transaction
    is set READ ONLY as its first statement, so any write in `work` fails.

    Args:
        pool: The connection pool to take the one connection from.
        work: The request's database work. It receives the connection.
        read_only: Whether to start the transaction READ ONLY.

    Returns:
        The value returned by `work`.

    Raises:
        ServiceBusyError: No connection became free within the pool timeout.
        LedgerInvariantError: The database raised an `IntegrityError` (a CHECK, foreign
            key, unique or similar constraint), whether during `work` or when the
            transaction commits. That is a bug the database safety net caught, so it is
            not a `LedgerError` and the API returns a logged 500. An expected conflict,
            such as a replayed idempotency key, must be handled inside `work` (for
            example with `ON CONFLICT`) or it ends up here.

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
