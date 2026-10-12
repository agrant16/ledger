from dataclasses import dataclass
from typing import Any, Sequence

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


PROBLEMS: dict[type[LedgerError], ProblemSpec] = {
    InvalidRequestError: ProblemSpec(400, "urn:ledger:error:invalid-request", "Invalid request"),
    UnknownAccountError: ProblemSpec(404, "urn:ledger:error:unknown-account", "Unknown account"),
    UnknownTransferError: ProblemSpec(404, "urn:ledger:error:unknown-transfer", "Unknown transfer"),
    CurrencyMismatchError: ProblemSpec(
        400, "urn:ledger:error:currency-mismatch", "Currency mismatch"
    ),
    InsufficientFundsError: ProblemSpec(
        422, "urn:ledger:error:insufficient-funds", "Insufficient funds"
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
        409, "urn:ledger:error:duplicate-account-name", "Duplicate account name"
    ),
    ServiceBusyError: ProblemSpec(503, "urn:ledger:error:service-busy", "Service busy"),
}

ERROR_LOC_MAPPING: dict[str, str] = {
    "header": "parameter",
    "body": "pointer",
    "query": "parameter",
    "path": "parameter",
    "cookie": "parameter",
}


def problem_for(error_class: type[Exception]) -> ProblemSpec:
    for cls in error_class.__mro__:
        if cls in PROBLEMS:
            return PROBLEMS[cls]
    raise LookupError(error_class)


def escape_pointer_tokens(pointer_tokens: Sequence[Any]) -> list[str]:
    return [str(token).replace("~", "~0").replace("/", "~1") for token in pointer_tokens]


def validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Converts Pydantic errors into the items of the RFC 9457 errors extension: detail plus a pointer or parameter
    :param errors: the Pydantic errors to convert.
    :return: Pydantic error formatted to RFC 9457-style error shape.
    """
    transformed_errors: list[dict[str, Any]] = []

    for error in errors:
        error_dict = {"detail": error.get("msg", "")}
        type_ = error.get("type", "")
        loc_ = error.get("loc", [])

        if type_ == "json_invalid":
            error_dict["pointer"] = ""
        elif loc_:
            loc_kind = ERROR_LOC_MAPPING.get(loc_[0], "")
            if loc_kind == "pointer":
                if len(loc_) >= 2:
                    error_dict["pointer"] = "".join(
                        "/" + token for token in escape_pointer_tokens(loc_[1:])
                    )
                else:
                    error_dict["pointer"] = ""
            elif loc_kind == "parameter" and len(loc_) >= 2:
                error_dict["parameter"] = ".".join(str(token) for token in loc_[1:])

        transformed_errors.append(error_dict)

    return transformed_errors
