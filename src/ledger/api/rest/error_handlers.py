import logging
import uuid
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from ledger.api.rest.middleware.concurrency_cap import ConcurrencyCapMiddleware
from ledger.api.rest.middleware.request_id import RequestIDMiddleware
from ledger.api.rest.problems import problem_for
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
    return [str(token).replace("~", "~0").replace("/", "~1") for token in pointer_tokens]


# check for status codes that don't allow bodies.
def is_body_allowed(status_code: int) -> bool:
    return not (100 <= status_code < 200 or status_code in (204, 205, 304))


def _locator(error: dict[str, Any]) -> dict[str, str]:
    """Where in the request the error is: a pointer, a parameter, or nothing."""
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
    """
    Converts Pydantic errors into the items of the RFC 9457 errors
    extension: detail plus a pointer or parameter
    """
    return [{"detail": error.get("msg", ""), **_locator(error)} for error in errors]


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
        logger.warning("Service was busy: %s", exc.__cause__ or exc)
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


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse | Response:
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
            exc.status_code,
            "about:blank",
            title,
            str(exc.detail),
            dict(exc.headers) if exc.headers else None,
        )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    if not request_id:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        request_id_ctx_var.set(request_id)
    logger.error("unhandled exception", exc_info=exc)
    return problem_response(
        request,
        500,
        "about:blank",
        "Internal Server Error",
        GENERIC_500_DETAIL,
        headers={"X-Request-Id": str(request_id)},
    )


def install_error_handling(app: FastAPI, concurrency_cap: int = CONCURRENCY_CAP) -> None:
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
