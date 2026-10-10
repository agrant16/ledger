class LedgerError(Exception):
    """Base class for expected, domain-level failures.

    These are normal outcomes of a request, not defects: unknown account,
    insufficient funds, a currency mismatch, an idempotency conflict, a busy
    database. The service layer raises them without any HTTP knowledge, and
    the API layer maps each subclass to a status code (4xx, or 503 for
    ServiceBusyError).

    Bugs are not LedgerErrors. See LedgerInvariantError, which returns a 500
    and is logged.
    """

    pass


class LedgerInvariantError(Exception):
    """A ledger invariant was violated by our own code, which means a bug.

    Deliberately not a LedgerError. LedgerError subclasses are expected
    outcomes (bad input, insufficient funds, a busy database) that the API
    maps to a 4xx or 503. This one signals a defect, so the API returns a
    500, the error is logged, and it is never retried.

    Example: the entries passed to post_transaction do not sum to zero per
    currency (UnbalancedEntriesError).
    """

    def __init__(self, message: str):
        super().__init__(message)


class DuplicateAccountNameError(LedgerError):
    def __init__(self, account_name: str):
        self.account_name = account_name
        super().__init__(f"Account with '{self.account_name}' already exists")


class InvalidRequestError(LedgerError):
    pass


class UnknownAccountError(LedgerError):
    pass


class UnknownTransferError(LedgerError):
    pass


class CurrencyMismatchError(LedgerError):
    pass


class InsufficientFundsError(LedgerError):
    def __init__(self, account_id, balance_minor, amount_minor):
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.amount_minor = amount_minor
        super().__init__(
            f"Insufficient funds: account {self.account_id}, current balance {self.balance_minor},"
            f" resulting balance {self.amount_minor}"
        )


class IdempotencyConflictError(LedgerError):
    pass


class AlreadyReversedError(LedgerError):
    pass


class ReversalOfReversalError(LedgerError):
    pass


class ServiceBusyError(LedgerError):
    pass


class UnbalancedEntriesError(LedgerInvariantError):
    pass
