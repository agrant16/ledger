from collections.abc import Callable
from functools import wraps

from ledger.errors import LedgerInvariantError


def transactional_service_method[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Refuses a connection that is None or in autocommit mode before calling the function.

    Service functions never commit, because the caller owns the transaction
    (ADR 0005). Calling one without a transaction in progress is a programming
    error, not a client error, so the wrapper raises LedgerInvariantError.

    The decorated function must take the connection as its first argument or as
    `conn`.

    Args:
        func: The service function to wrap.

    Returns:
        The wrapped function, which raises before calling `func` if the connection is
        missing or in autocommit mode.

    Raises:
        LedgerInvariantError: A connection was not given or it is in autocommit mode.
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
