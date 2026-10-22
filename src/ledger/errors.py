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


class LedgerInvariantError(Exception):
    """A ledger invariant was violated, which means a bug.

    Deliberately not a LedgerError. LedgerError subclasses are expected
    outcomes (bad input, insufficient funds, a busy database) that the API
    maps to a 4xx or 503. This one signals a defect, so the API returns a
    500, the error is logged, and it is never retried. psycopg.errors.IntegrityError
    also maps to LedgerInvariantError as they should not happen if database
    operations are written correctly.

    Example: the entries passed to post_transaction do not sum to zero per
    currency (UnbalancedEntriesError).

    Example: An account does not have a balance row. This violates a database constraint
    as the account creation also writes a corresponding balances row. If it doesn't exist,
    then something did not work correctly within the service.
    """


class DuplicateAccountNameError(LedgerError):
    """An account with the desired name already exists in the database."""

    def __init__(self, account_name: str):
        self.account_name = account_name
        super().__init__(f"Account with name '{self.account_name}' already exists")


class InvalidRequestError(LedgerError):
    """The request failed validation. Maps to 400, not FastAPI's default 422."""


class UnknownAccountError(LedgerError):
    pass


class UnknownTransferError(LedgerError):
    pass


class CurrencyMismatchError(LedgerError):
    """A transfer or entry's currency differs from the account's currency.
    Defends rule 6 from the design doc."""


class InsufficientFundsError(LedgerError):
    """A non-funding account would go negative (rule 4). Maps to 422.

    Args:
        account_id: The account id of the sender
        balance_minor: The current balance of the sender
        net_change_minor: The net change that would be applied to the sender balance

    Example: Non-funding account A transfers 5000 to non-funding account B. A's
    current balance is 4000. The net change is -5000 which results in a new balance of
    -1000. This raises an InsufficientFundsError.
    """

    def __init__(self, account_id: int, balance_minor: int, net_change_minor: int):
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.net_change_minor = net_change_minor
        super().__init__(
            f"Insufficient funds: account {self.account_id}, current balance {self.balance_minor},"
            f" net change {self.net_change_minor}"
        )


class IdempotencyConflictError(LedgerError):
    """Existing Idempotency key reused with a different request. The request hash doesn't match.
    Defends rule 5 from the design doc.
    """


class AlreadyReversedError(LedgerError):
    """A reversal is requested on a transfer that has already been reversed."""


class ReversalOfReversalError(LedgerError):
    """The transfer to be reversed is itself a reversal."""


class ServiceBusyError(LedgerError):
    """No pooled connection is free within pool timeout. Returns 503 and safe for retry."""


class UnbalancedEntriesError(LedgerInvariantError):
    """The sum of entries per currency in a transfer is not zero. A bug.
    Defends rule 1 from the design doc."""
