from functools import wraps

from ledger.errors import LedgerInvariantError


def transactional_service_method(func):
    @wraps(func)
    def require_transaction(*args, **kwargs):
        """Caller makes commit so disallow autocommit in service methods."""
        if "conn" in kwargs:
            conn = kwargs["conn"]
        elif args:
            conn = args[0]
        else:
            conn = None

        if not conn:
            raise LedgerInvariantError(
                f"{func.__name__} needs a connection with a transaction in progress"
            )
        if conn.autocommit:
            raise LedgerInvariantError(
                f"{func.__name__} needs a connection with a transaction in progress"
            )
        return func(*args, **kwargs)

    return require_transaction
