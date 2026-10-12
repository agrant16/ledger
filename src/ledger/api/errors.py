import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from ledger.api.request_id import (
    RequestIDMiddleware,
    install_log_record_factory,
)
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


logger: logging.Logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 10

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
    Converts Pydantic errors into the items of the RFC 9457 errors
    extension: detail plus a pointer or parameter

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


def problem_response(
    request: Request,
    status: int,
    type_: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body = {
        "title": title,
        "type": type_,
        "detail": detail,
        "status": status,
        "instance": f"urn:uuid:{request.state.request_id}",
        **(extensions or {}),
    }
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    problem_spec = problem_for(exc.__class__)
    headers = None
    extensions = {name: getattr(exc, name) for name in problem_spec.extensions}
    if problem_spec.status == 503:
        headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
        logger.warning(
            f"{type(exc).__name__} occurrence, Retry-After header added with {RETRY_AFTER_SECONDS}"
        )
    return problem_response(
        request,
        problem_spec.status,
        problem_spec.type,
        problem_spec.title,
        str(exc),
        headers,
        extensions,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    spec = problem_for(InvalidRequestError)
    return problem_response(
        request,
        spec.status,
        spec.type,
        spec.title,
        "The request is not valid.",  # a fixed sentence; the specifics are in `errors`
        headers=None,
        extensions={"errors": validation_errors(exc.errors())},
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    pass


async def exception_handler(request: Request, exc: Exception) -> JSONResponse:
    pass


def install_error_handling(app: FastAPI) -> None:
    install_log_record_factory()
    app.add_middleware(RequestIDMiddleware)
    # Starlette passes only LedgerError to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(LedgerError, ledger_error_handler)
    app.add_exception_handler(Exception, exception_handler)
    # Starlette passes only RequestValidationError to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Starlette passes only HTTPException to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(HTTPException, http_error_handler)
