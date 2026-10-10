import psycopg
from psycopg_pool import ConnectionPool

from ledger.config import Settings
from ledger.errors import LedgerInvariantError


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


def require_transaction(conn: psycopg.Connection, method_name: str) -> None:
    """Caller makes commit so disallow autocommit in service methods."""
    if conn.autocommit:
        raise LedgerInvariantError(
            f"{method_name} needs a connection with a transaction in progress"
        )
