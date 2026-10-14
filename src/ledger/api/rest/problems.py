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
        extensions=("account_id", "balance_minor", "amount_minor"),
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
    for cls in error_class.__mro__:
        if cls in PROBLEMS:
            return PROBLEMS[cls]
    raise LookupError(error_class)
