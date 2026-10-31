import random
import time
from collections.abc import Callable
from contextlib import ExitStack

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ledger.errors import LedgerInvariantError, ServiceBusyError
from ledger.infra.config import Settings

MAX_ATTEMPTS = 3
MIN_DELAY_SECONDS = 0.010
MAX_DELAY_SECONDS = 0.050

Pool = ConnectionPool


def create_pool(settings: Settings) -> Pool:
    """Creates a fixed-size pool of app-role connections that never grows.

    A request must use one connection for its whole transaction and never take a second
    while holding the first.

    Args:
        settings: The service settings (app-role URL, pool size and wait timeout).

    Returns:
        A pool that is not yet open. A caller waits up to the configured timeout for a free
        connection.
    """
    return Pool(
        settings.app_url,
        min_size=settings.pool_size,
        max_size=settings.pool_size,
        timeout=settings.pool_timeout_seconds,
        open=False,
    )


def _acquire(pool: Pool, stack: ExitStack) -> psycopg.Connection:
    """Acquires a connection from the pool, raising `ServiceBusyError` on a wait timeout."""
    try:
        return stack.enter_context(pool.connection())
    except PoolTimeout as err:
        raise ServiceBusyError("Connection timeout") from err


def run_in_transaction[T](
    pool: Pool,
    work: Callable[[psycopg.Connection], T],
    *,
    read_only: bool = False,
) -> T | None:
    """
    Runs `work` on a pooled connection in one database transaction. (rule 5, ADR 0011, ADR 0017)

    This is where a request's transaction starts and ends. Service functions take the
    connection and never commit (the caller owns the transaction), so everything `work`
    does either commits together when it returns or rolls back together when it raises.

    `work` receives the one connection for the whole request. It must not commit or roll
    back, and it must not take a second connection from the pool while holding this one.

    A write that the database aborts as a deadlock victim (SQLSTATE 40P01) is rolled back
    and `work` is called again on a fresh transaction, up to 3 attempts in all, with a
    random delay of 10 to 50 ms between attempts. `work` must therefore be safe to run
    again from its start, including the idempotency key claim. Read-only runs and every
    other error are not retried.

    The isolation level is the default, READ COMMITTED. With `read_only`, the transaction
    is set READ ONLY as its first statement, so any write in `work` fails.

    Args:
        pool: The connection pool to take the one connection from.
        work: The request's database work. It receives the connection.
        read_only: Whether to start the transaction READ ONLY.

    Returns:
        The value returned by `work` in the attempt that committed.

    Raises:
        ServiceBusyError: No connection became free within the pool timeout, or every
            attempt was a deadlock victim, in which case the last deadlock error is the
            cause.
        LedgerInvariantError: The database raised an `IntegrityError` (a CHECK, foreign
            key, unique or similar constraint), whether during `work` or when the
            transaction commits. A deadlock occurred on a read only transaction. That is
            a bug the database safety net caught, so it is not a `LedgerError` and the API
            returns a logged 500. An expected conflict, such as a replayed idempotency key,
            must be handled inside `work` (for example with `ON CONFLICT`) or it ends up here.
        LedgerError: Any non-bug or service-busy LedgerError and subclasses raised by `work`.
    """
    with ExitStack() as stack:
        conn = _acquire(pool, stack)
        for attempt in range(MAX_ATTEMPTS):
            try:
                with conn.transaction():
                    if read_only:
                        conn.execute("SET TRANSACTION READ ONLY")
                    return work(conn)
            except psycopg.errors.IntegrityError as failure:
                raise LedgerInvariantError(
                    f"A database constraint was violated: ({failure.diag.constraint_name})"
                ) from failure
            except psycopg.errors.DeadlockDetected as failure:
                if read_only:
                    raise LedgerInvariantError(
                        "Deadlock detected on a read only transaction"
                    ) from failure
                if attempt == MAX_ATTEMPTS - 1:
                    raise ServiceBusyError("Transaction deadlock detected") from failure
                time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
    return None