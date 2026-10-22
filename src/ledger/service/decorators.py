from collections.abc import Callable
from functools import wraps

from ledger.errors import LedgerInvariantError


def transactional_service_method[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Refuse a connection that is None or in autocommit mode before calling the function.

    Service functions never commit, because the caller owns the transaction
    (ADR 0005). In autocommit mode each statement would commit on its own, so
    a failure partway through a posting could not be rolled back. The wrapper
    raises LedgerInvariantError instead, since this is a programming error and
    not a client error.

    The connection is the `conn` keyword argument if given, otherwise the
    first positional argument, so every decorated function must take the
    connection first.
    """

    @wraps(func)
    def require_transaction(*args, **kwargs):
        conn = kwargs["conn"] if "conn" in kwargs else (args[0] if args else None)
        if conn is None or conn.autocommit:
            raise LedgerInvariantError(
                f"{func.__name__} needs a connection with a transaction in progress"
            )
        return func(*args, **kwargs)

    return require_transaction
