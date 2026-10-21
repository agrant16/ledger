from collections.abc import Callable
from functools import wraps

from ledger.errors import LedgerInvariantError


def transactional_service_method[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Refuse a connection that is None or in autocommit mode before calling the function."""
    @wraps(func)
    def require_transaction(*args, **kwargs):
        """Caller makes commit so disallow autocommit in service methods."""
        conn = kwargs["conn"] if "conn" in kwargs else (args[0] if args else None)
        if conn is None or conn.autocommit:
            raise LedgerInvariantError(
                f"{func.__name__} needs a connection with a transaction in progress"
            )
        return func(*args, **kwargs)

    return require_transaction
