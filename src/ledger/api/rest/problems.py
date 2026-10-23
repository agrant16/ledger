from dataclasses import dataclass

from ledger.errors import (
    AlreadyReversedError,
    CurrencyMismatchError,
    DuplicateAccountNameError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    LedgerError,
    ReversalOfReversalError,
    ServiceBusyError,
    UnknownAccountError,
    UnknownTransferError,
)


@dataclass(frozen=True)
class ProblemSpec:
    """How a `LedgerError` is reported: the fixed fields of the problem body. (ADR 0009)

    Attributes:
        status (int): The HTTP status code.
        type (str): The problem `type` URI, such as `urn:ledger:error:unknown-account`.
        title (str): The short, fixed problem title.
        extensions (tuple[str, ...]): The names of the exception attributes copied into the
            problem body as extra members.
    """

    status: int
    type: str
    title: str
    extensions: tuple[str, ...] = ()


PROBLEMS: dict[type[LedgerError], ProblemSpec] = {
    InvalidRequestError: ProblemSpec(400, "urn:ledger:error:invalid-request", "Invalid request"),
    UnknownAccountError: ProblemSpec(404, "urn:ledger:error:unknown-account", "Unknown account"),
    UnknownTransferError: ProblemSpec(404, "urn:ledger:error:unknown-transfer", "Unknown transfer"),
    CurrencyMismatchError: ProblemSpec(
        400, "urn:ledger:error:currency-mismatch", "Currency mismatch"
    ),
    InsufficientFundsError: ProblemSpec(
        422,
        "urn:ledger:error:insufficient-funds",
        "Insufficient funds",
        extensions=("account_id", "balance_minor", "net_change_minor"),
    ),
    IdempotencyConflictError: ProblemSpec(
        409, "urn:ledger:error:idempotency-conflict", "Idempotency key reused"
    ),
    AlreadyReversedError: ProblemSpec(
        409, "urn:ledger:error:already-reversed", "Transfer already reversed"
    ),
    ReversalOfReversalError: ProblemSpec(
        409, "urn:ledger:error:reversal-of-reversal", "A reversal cannot be reversed"
    ),
    DuplicateAccountNameError: ProblemSpec(
        409,
        "urn:ledger:error:duplicate-account-name",
        "Duplicate account name",
        extensions=("account_name",),
    ),
    ServiceBusyError: ProblemSpec(503, "urn:ledger:error:service-busy", "Service busy"),
}


def problem_for(error_class: type[Exception]) -> ProblemSpec:
    """Returns the problem spec for an error class.

    The class's own entry is used, otherwise the entry of its nearest mapped base class.

    Args:
        error_class: The exception class to look up.

    Returns:
        The spec of the class or of its nearest mapped base class.

    Raises:
        LookupError: Neither the class nor any of its bases is mapped.
    """
    for cls in error_class.__mro__:
        if cls in PROBLEMS:
            return PROBLEMS[cls]
    raise LookupError(error_class)
