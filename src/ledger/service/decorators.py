from functools import wraps

from ledger.errors import LedgerInvariantError


def transactional_service_method(func):
    @wraps(func)
    def require_transaction(*args, **kwargs):
        """Caller makes commit so disallow autocommit in service methods."""
        conn = kwargs.get("conn")
        if conn is None and args:
            conn = args[0]

        if conn is not None and getattr(conn, "autocommit", False):
            raise LedgerInvariantError(
                f"{func.__name__} needs a connection with a transaction in progress"
            )
        return func(*args, **kwargs)

    return require_transaction
