class LedgerError(Exception):
    """Base class for expected, domain-level failures.

    The service layer raises these without HTTP knowledge, and the API layer maps each
    subclass to a status code. Bugs are not `LedgerError`s; see `LedgerInvariantError`.
    """


class LedgerInvariantError(Exception):
    """A ledger invariant was violated, which means a bug.

    Not a `LedgerError`: the API returns a logged 500 and never retries it. Raised for
    unbalanced entries (`UnbalancedEntriesError`) and when `run_in_transaction` converts a
    database safety-net `IntegrityError`.
    """


class DuplicateAccountNameError(LedgerError):
    """An account with this name already exists.

    Attributes:
        account_name (str): The name that is already taken.
    """

    def __init__(self, account_name: str) -> None:
        self.account_name = account_name
        super().__init__(f"Account with name '{self.account_name}' already exists")


class InvalidRequestError(LedgerError):
    """The request failed validation. Maps to 400, not FastAPI's default 422."""


class UnknownAccountError(LedgerError):
    """The account ID provided was not found in the database."""


class UnknownTransferError(LedgerError):
    """The transfer ID provided was not found in the database."""


class CurrencyMismatchError(LedgerError):
    """A transfer or entry's currency differs from the account's currency. (rule 6)"""


class InsufficientFundsError(LedgerError):
    """A non-funding account would go negative (rule 4).

    Attributes:
        account_id (int): The id of the account that would go negative.
        balance_minor (int): That account's current balance.
        net_change_minor (int): The net change the transfer would make to that balance.
    """

    def __init__(self, account_id: int, balance_minor: int, net_change_minor: int) -> None:
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.net_change_minor = net_change_minor
        super().__init__(
            f"Insufficient funds: account {self.account_id}, current balance {self.balance_minor},"
            f" net change {self.net_change_minor}"
        )


class IdempotencyConflictError(LedgerError):
    """Existing idempotency key reused with a different request (rule 5)."""


class AlreadyReversedError(LedgerError):
    """A reversal is requested on a transfer that has already been reversed."""


class ReversalOfReversalError(LedgerError):
    """The transfer to be reversed is itself a reversal."""


class ServiceBusyError(LedgerError):
    """No pooled connection is free within the pool timeout. This is safe for retry."""


class UnbalancedEntriesError(LedgerInvariantError):
    """The sum of entries per currency in a transfer is not zero (rule 1). A bug."""


class NotReadyError(LedgerError):
    """The service cannot do its job: the database is unreachable or read-only, or a
    required table or privilege is missing."""
