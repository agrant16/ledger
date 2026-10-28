from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from ledger.errors import (
    AlreadyReversedError,
    CurrencyMismatchError,
    DuplicateAccountNameError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    LedgerError,
    NotReadyError,
    ReversalOfReversalError,
    ServiceBusyError,
    UnknownAccountError,
    UnknownTransferError,
)


@dataclass(frozen=True)
class ProblemSpec:
    """How a `LedgerError` is reported: the fixed fields of the problem body. (ADR 0009)

    Attributes:
        status: The HTTP status code.
        type: The problem `type` URI, such as `urn:ledger:error:unknown-account`.
        title: The short, fixed problem title.
        extensions: The names of the exception attributes copied into the
            problem body as extra members.
        retry_after: `True` if the problem response should send a `Retry-After` header,
            `False` otherwise.
    """

    status: int
    type: str
    title: str
    extensions: tuple[str, ...] = ()
    retry_after: bool = False


@dataclass(frozen=True)
class ProblemOccurrence:
    """The parts of a problem response that vary between occurrences. (ADR 0009)

    Attributes:
        detail: The details of the problem occurrence.
        headers: The headers to return with the problem response.
        extensions: The extension attributes associated with the problem occurrence.
    """

    detail: str
    headers: dict[str, str] | None = None
    extensions: Mapping[str, Any] | None = None


INTERNAL_ERROR = ProblemSpec(
    status=HTTPStatus.INTERNAL_SERVER_ERROR,
    type="about:blank",
    title="Internal Server Error",
)


PROBLEMS: dict[type[LedgerError], ProblemSpec] = {
    InvalidRequestError: ProblemSpec(
        HTTPStatus.BAD_REQUEST, "urn:ledger:error:invalid-request", "Invalid request"
    ),
    UnknownAccountError: ProblemSpec(
        HTTPStatus.NOT_FOUND, "urn:ledger:error:unknown-account", "Unknown account"
    ),
    UnknownTransferError: ProblemSpec(
        HTTPStatus.NOT_FOUND, "urn:ledger:error:unknown-transfer", "Unknown transfer"
    ),
    CurrencyMismatchError: ProblemSpec(
        HTTPStatus.BAD_REQUEST, "urn:ledger:error:currency-mismatch", "Currency mismatch"
    ),
    InsufficientFundsError: ProblemSpec(
        HTTPStatus.UNPROCESSABLE_CONTENT,
        "urn:ledger:error:insufficient-funds",
        "Insufficient funds",
        extensions=("account_id", "balance_minor", "net_change_minor"),
    ),
    IdempotencyConflictError: ProblemSpec(
        HTTPStatus.CONFLICT, "urn:ledger:error:idempotency-conflict", "Idempotency key reused"
    ),
    AlreadyReversedError: ProblemSpec(
        HTTPStatus.CONFLICT, "urn:ledger:error:already-reversed", "Transfer already reversed"
    ),
    ReversalOfReversalError: ProblemSpec(
        HTTPStatus.CONFLICT,
        "urn:ledger:error:reversal-of-reversal",
        "A reversal cannot be reversed",
    ),
    DuplicateAccountNameError: ProblemSpec(
        HTTPStatus.CONFLICT,
        "urn:ledger:error:duplicate-account-name",
        "Duplicate account name",
        extensions=("account_name",),
    ),
    ServiceBusyError: ProblemSpec(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "urn:ledger:error:service-busy",
        "Service busy",
        retry_after=True,
    ),
    NotReadyError: ProblemSpec(
        HTTPStatus.SERVICE_UNAVAILABLE, "urn:ledger:error:not-ready", "Not ready"
    ),
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
