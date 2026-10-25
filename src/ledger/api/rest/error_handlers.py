import logging
import uuid
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from ledger.api.rest.middleware.concurrency_cap import ConcurrencyCapMiddleware
from ledger.api.rest.middleware.request_id import RequestIDMiddleware
from ledger.api.rest.problems import ProblemOccurrence, ProblemSpec, problem_for
from ledger.errors import InvalidRequestError, LedgerError
from ledger.infra.config import CONCURRENCY_CAP
from ledger.infra.logging_config import install_log_record_factory, request_id_ctx_var

logger: logging.Logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 10

GENERIC_500_DETAIL = "An unexpected error occurred. Include the request id when reporting it."


ERROR_LOC_MAPPING: dict[str, str] = {
    "header": "parameter",
    "body": "pointer",
    "query": "parameter",
    "path": "parameter",
    "cookie": "parameter",
}


def _escape_pointer_tokens(pointer_tokens: Sequence[Any]) -> list[str]:
    """Escapes `~` and `/` in JSON Pointer tokens (RFC 6901), so each stays one path segment."""
    return [str(token).replace("~", "~0").replace("/", "~1") for token in pointer_tokens]


def is_body_allowed(status_code: int) -> bool:
    """Returns whether a response with this status may have a body.

    Informational (1xx), 204, 205 and 304 responses may not.

    Args:
        status_code: The HTTP status code.

    Returns:
        `True` when a body is allowed.
    """
    return not (100 <= status_code < 200 or status_code in (204, 205, 304))


def _locator(error: dict[str, Any]) -> dict[str, str]:
    """Returns where in the request a Pydantic error is: a pointer, a parameter, or nothing.

    Args:
        error: One item of a Pydantic error list.

    Returns:
        `{"pointer": ...}` for the body (a JSON Pointer, empty when the body is not valid JSON),
        `{"parameter": ...}` for a header, query, path or cookie parameter, otherwise `{}`.
    """
    if error.get("type") == "json_invalid":
        return {"pointer": ""}
    loc = error.get("loc", [])
    if not loc:
        return {}
    where, *path = loc
    kind = ERROR_LOC_MAPPING.get(where)
    if kind == "pointer":
        return {"pointer": "".join(f"/{token}" for token in _escape_pointer_tokens(path))}
    if kind == "parameter" and path:
        return {"parameter": ".".join(str(token) for token in path)}
    return {}


def validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converts Pydantic errors into the items of the `errors` problem extension. (ADR 0009)

    Args:
        errors: The error list of a `RequestValidationError`.

    Returns:
        One item per error: its `detail` message plus a `pointer` or `parameter` when the
        location is known.
    """
    return [{"detail": error.get("msg", ""), **_locator(error)} for error in errors]


def problem_response(
    request: Request, spec: ProblemSpec, occurrence: ProblemOccurrence
) -> JSONResponse:
    """Builds an RFC 9457 problem response. (ADR 0009)

    The body has `title`, `type`, `detail`, `status` and `instance`, which is
    `urn:uuid:<request id>`, plus the extension members. The media type is
    `application/problem+json`.

    Args:
        request: The request being answered. Its `state.request_id` must be set.
        spec: The problem status, type, and title.
        occurrence: The detail, headers, and extension members for this response.

    Returns:
        The response.
    """
    body = {
        "title": spec.title,
        "type": spec.type,
        "detail": occurrence.detail,
        "status": spec.status,
        "instance": f"urn:uuid:{request.state.request_id}",
        **(occurrence.extensions or {}),
    }
    return JSONResponse(
        body,
        status_code=spec.status,
        headers=occurrence.headers,
        media_type="application/problem+json",
    )


async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    """Turns a `LedgerError` into its problem response. (ADR 0007, ADR 0009)

    The status, `type` and `title` come from the error class's `ProblemSpec`. The `detail` is
    the error's message. The members the spec lists as extensions, such as `account_id` for an
    insufficient-funds error, are copied from the error into the body. A 503 also carries a
    `Retry-After` header.

    Args:
        request: The request being answered.
        exc: The error raised by the handler or the services.

    Returns:
        The problem response.

    Raises:
        LookupError: The error class has no `ProblemSpec`, which is a bug.
    """
    problem_spec = problem_for(exc.__class__)
    headers = None
    extensions = {name: getattr(exc, name) for name in problem_spec.extensions}
    if problem_spec.title == "Service busy":
        headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
        logger.warning("Service was busy: %s", exc.__cause__ or exc)
    if problem_spec.title == "Not ready":
        logger.warning("Not ready: %s", exc.__cause__ or exc)
    return problem_response(
        request,
        problem_spec,
        ProblemOccurrence(detail=str(exc), headers=headers, extensions=extensions),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Turns a request validation failure into a 400 problem response. (ADR 0009)

    The body lists each failure, with its location, in the `errors` member.

    Args:
        request: The request being answered.
        exc: The validation error raised by FastAPI.

    Returns:
        The 400 problem response.
    """
    problem_spec = problem_for(InvalidRequestError)
    return problem_response(
        request,
        problem_spec,
        ProblemOccurrence(
            detail="The request is not valid.",
            extensions={"errors": validation_errors(exc.errors())},
        ),
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse | Response:
    """Turns a framework HTTP error, such as a 404 or 405, into a problem response. (ADR 0009)

    The `type` is `about:blank`, the `title` is the standard phrase for the status, and the
    exception's headers are kept. A status that may not have a body gets an empty response.

    Args:
        request: The request being answered.
        exc: The HTTP error raised by Starlette.

    Returns:
        The problem response, or an empty response when the status allows no body.
    """
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "HTTP Error"  # generic title for anything not recognized

    if not is_body_allowed(exc.status_code):
        return Response(
            status_code=exc.status_code,
            headers=exc.headers,
        )
    else:
        return problem_response(
            request,
            ProblemSpec(status=exc.status_code, type="about:blank", title=title),
            ProblemOccurrence(
                detail=str(exc.detail), headers=dict(exc.headers) if exc.headers else None
            ),
        )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Turns a bug into a generic 500 problem response and logs it. (ADR 0007, ADR 0009)

    The body has a fixed message that reveals nothing about the error. The request id is in
    `instance` and in the `X-Request-Id` header, and the error is logged with its traceback
    under that id. A request that has no id yet is given one.

    Args:
        request: The request being answered.
        exc: The unexpected exception.

    Returns:
        The 500 problem response.
    """
    request_id = getattr(request.state, "request_id", None)
    if not request_id:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        request_id_ctx_var.set(request_id)
    logger.error("unhandled exception", exc_info=exc)
    return problem_response(
        request,
        ProblemSpec(
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            type="about:blank",
            title="Internal Server Error",
        ),
        ProblemOccurrence(detail=GENERIC_500_DETAIL, headers={"X-Request-Id": str(request_id)}),
    )


def install_error_handling(app: FastAPI, concurrency_cap: int = CONCURRENCY_CAP) -> None:
    """Installs the request id and concurrency cap middleware and the error handlers. (ADR 0009)

    Every response, including the cap's 503, carries an `X-Request-Id` header. The handlers
    cover `LedgerError` (its mapped status), `RequestValidationError` (400), Starlette's
    `HTTPException` (its own status) and any other `Exception` (500).

    Args:
        app: The application to install them on.
        concurrency_cap: The number of HTTP requests allowed in flight at once; a request over
            the cap gets a 503.
    """
    install_log_record_factory()
    # ConcurrencyCapMiddleware must be added before RequestIDMiddleware so
    # RequestIDMiddleware remains the outer scope. This allows the response from
    #  ConcurrencyCapMiddleware to obtain a request id.
    app.add_middleware(ConcurrencyCapMiddleware, cap=concurrency_cap)
    app.add_middleware(RequestIDMiddleware)
    # Starlette passes only LedgerError to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(LedgerError, ledger_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    # Starlette passes only RequestValidationError to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Starlette passes only HTTPException to this handler
    # noinspection PyTypeChecker
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
