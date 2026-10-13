from collections.abc import Callable

import psycopg
from psycopg_pool import ConnectionPool

from ledger.errors import LedgerInvariantError
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


def run_in_transaction[T](
    pool: ConnectionPool,
    work: Callable[[psycopg.Connection], T],
    *,
    read_only: bool = False,
) -> T:
    try:
        with pool.connection() as conn:
            if read_only:
                conn.execute("SET TRANSACTION READ ONLY")
            return work(conn)
    except psycopg.errors.IntegrityError as failure:
        raise LedgerInvariantError(
            f"A database constraint was violated: ({failure.diag.constraint_name})"
        ) from failure
