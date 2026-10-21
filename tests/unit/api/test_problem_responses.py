"""Problem responses end to end (docs/design.md Error model; ADR 0007; milestone 3 plan).

Every failure leaves the API as an RFC 9457 `application/problem+json` body. These tests run a
throwaway FastAPI app whose only job is to raise things, so they need no database.

Contract these tests expect from ledger/api/rest/error_handlers.py:

    install_error_handling(app: FastAPI) -> None

It registers, on the given app:
- a handler for LedgerError, using the error table: status, `type`, `title`, `detail` (the
  exception's message), `instance` (`urn:uuid:<request id>`), plus the data the error carries as
  extension members. A 503 also sends a Retry-After header;
- a handler for RequestValidationError: status 400 (never FastAPI's 422), type
  `urn:ledger:error:invalid-request`, and the `errors` list from `validation_errors`;
- a handler for HTTP errors raised by Starlette or a route (an unknown route, a wrong method, an
  `HTTPException`): the same RFC 9457 shape with type `about:blank`, the HTTP status phrase as the
  `title`, the exception's own `detail`, and any headers it carries (a 405 keeps its `Allow`);
- one catch-all handler for everything else: a bug. It returns status 500 with type `about:blank`,
  the title "Internal Server Error" and a fixed generic `detail` that never repeats the exception.
  It logs the exception at ERROR, once;
- middleware that gives every response an `X-Request-ID` header (a server-generated UUID), and
  puts the same id on every log record emitted during the request as `record.request_id`.

Logging is deliberately minimal: a bug is logged at ERROR, a 503 at WARNING, and the other expected
errors are not logged as errors.
"""

import asyncio
import json
import logging
import uuid
from http import HTTPStatus
from typing import Annotated

import psycopg
import pytest
from fastapi import FastAPI, Header, HTTPException, Path, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from starlette.requests import Request

from ledger.api.rest.error_handlers import install_error_handling, unhandled_exception_handler
from ledger.errors import LedgerInvariantError, ServiceBusyError, UnbalancedEntriesError
from ledger.infra.logging_config import install_log_record_factory
from tests.unit.api.api_cases import EXAMPLES, EXPECTED, EXTENSIONS, STANDARD_MEMBERS

LEDGER_ERRORS = {error.__name__: error for error in EXPECTED}

BUGS = {
    "key-error": lambda: KeyError("secret-key"),
    "database-check": lambda: psycopg.errors.CheckViolation(
        'new row violates check constraint "secret_constraint"'
    ),
    "invariant": lambda: LedgerInvariantError("secret invariant"),
    "unbalanced": lambda: UnbalancedEntriesError("secret unbalanced"),
    "runtime": lambda: RuntimeError("secret runtime"),
}

GOOD_TRANSFER = {"from_account_id": 1, "to_account_id": 2, "amount_minor": 5, "currency": "USD"}
KEY = {"Idempotency-Key": "key-1"}


class TransferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account_id: StrictInt = Field(ge=1)
    to_account_id: StrictInt = Field(ge=1)
    amount_minor: StrictInt = Field(ge=1, le=99_900_000_000)
    currency: str = Field(pattern="^[A-Z]{3}$")

    @model_validator(mode="after")
    def accounts_differ(self) -> "TransferBody":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from and to must be different accounts")
        return self


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handling(app)

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/raise-ledger/{name}")
    def raise_ledger(name: str) -> None:
        raise EXAMPLES[LEDGER_ERRORS[name]]()

    @app.get("/raise-busy-with-cause")
    def raise_busy_with_cause() -> None:
        raise ServiceBusyError("Connection timeout") from TimeoutError("the pool gave up after 2s")

    @app.get("/raise-bug/{name}")
    def raise_bug(name: str) -> None:
        raise BUGS[name]()

    @app.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @app.get("/http-error/{status}")
    def http_error(status: int) -> None:
        raise HTTPException(status_code=status, detail="raised by a route")

    @app.get("/http-error-with-a-dict")
    def http_error_with_a_dict() -> None:
        raise HTTPException(status_code=400, detail={"why": "because"})

    @app.get("/log")
    def log_something() -> dict[str, bool]:
        logging.getLogger("ledger.test").info("inside a request")
        return {"ok": True}

    @app.post("/transfer")
    def transfer(
        body: TransferBody,
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/accounts/{account_id}/entries")
    def entries(
        account_id: Annotated[int, Path(ge=1)], limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> dict[str, bool]:
        return {"ok": True}

    # Starlette's TestClient re-raises server errors into the test by default, even when a handler
    # has already turned them into a 500 response.
    return TestClient(app, raise_server_exceptions=False)


def problem(response) -> dict:
    assert response.headers["content-type"].startswith("application/problem+json")
    return json.loads(response.content)


def request_id(response) -> str:
    value = response.headers["X-Request-ID"]
    uuid.UUID(value)  # raises unless it is a UUID
    return value


def at_least(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= level]


# --- LedgerError -> problem ---------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LEDGER_ERRORS))
def test_a_ledger_error_becomes_a_problem_response(client, caplog, name) -> None:
    caplog.set_level(logging.DEBUG)
    error = LEDGER_ERRORS[name]
    status, type_, title = EXPECTED[error]
    extensions = EXTENSIONS.get(error, {})

    response = client.get(f"/raise-ledger/{name}")
    body = problem(response)

    assert response.status_code == status
    assert body["status"] == status
    assert (body["type"], body["title"]) == (type_, title)
    assert body["detail"] == str(EXAMPLES[error]())
    assert set(body) == STANDARD_MEMBERS | set(extensions)
    for key, value in extensions.items():
        assert body[key] == value
    assert body["instance"] == f"urn:uuid:{request_id(response)}"
    assert ("Retry-After" in response.headers) == (status == 503)
    assert not at_least(caplog, logging.ERROR)


def test_service_busy_says_when_to_retry_and_is_logged_as_a_warning(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    response = client.get("/raise-ledger/ServiceBusyError")

    assert int(response.headers["Retry-After"]) >= 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].request_id == request_id(response)
    assert not at_least(caplog, logging.ERROR)


def test_the_warning_for_a_503_carries_its_cause(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.get("/raise-busy-with-cause")

    assert response.status_code == 503
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "the pool gave up after 2s" in warnings[0].getMessage()
    assert "the pool gave up" not in response.text  # the cause is for the log, not the client


# --- bugs -> a generic 500 -----------------------------------------------------------------------


@pytest.mark.parametrize("bug", sorted(BUGS))
def test_a_bug_becomes_a_generic_500_and_is_logged_once(client, caplog, bug) -> None:
    caplog.set_level(logging.DEBUG)
    response = client.get(f"/raise-bug/{bug}")
    body = problem(response)

    assert response.status_code == 500
    assert body["status"] == 500
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert set(body) == STANDARD_MEMBERS
    assert "secret" not in response.text, "the response leaked the exception"
    assert body["instance"] == f"urn:uuid:{request_id(response)}"

    errors = at_least(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is type(BUGS[bug]())
    assert errors[0].request_id == request_id(response)


def test_the_500_detail_is_the_same_whatever_went_wrong(client) -> None:
    details = {problem(client.get(f"/raise-bug/{name}"))["detail"] for name in BUGS}
    assert len(details) == 1


def test_the_catch_all_makes_an_id_when_the_request_has_none(caplog) -> None:
    """The middleware always runs first, so this cannot happen in the app. The catch-all is the last
    resort and must not fail itself, so it covers a request that reached it without an id."""
    install_log_record_factory()  # normally done by install_error_handling
    caplog.set_level(logging.DEBUG)
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": []})

    response = asyncio.run(unhandled_exception_handler(request, RuntimeError("secret")))
    body = json.loads(response.body)

    assert response.status_code == 500
    assert response.media_type == "application/problem+json"
    header_id = request_id(response)
    assert body["instance"] == f"urn:uuid:{header_id}"
    assert body["type"] == "about:blank"
    assert "secret" not in response.body.decode()
    errors = at_least(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].request_id == header_id


# --- validation errors -> 400 with an errors list ------------------------------------------------

VALIDATION_CASES = {
    "missing fields and header": (
        lambda c: c.post("/transfer", json={"from_account_id": 1}),
        {
            ("pointer", "/to_account_id"),
            ("pointer", "/amount_minor"),
            ("pointer", "/currency"),
            ("parameter", "Idempotency-Key"),
        },
    ),
    "unknown field": (
        lambda c: c.post("/transfer", json={**GOOD_TRANSFER, "ammount_minor": 1}, headers=KEY),
        {("pointer", "/ammount_minor")},
    ),
    "amount as a string": (
        lambda c: c.post(
            "/transfer", json={**GOOD_TRANSFER, "amount_minor": "secret-value"}, headers=KEY
        ),
        {("pointer", "/amount_minor")},
    ),
    "same account": (
        lambda c: c.post("/transfer", json={**GOOD_TRANSFER, "to_account_id": 1}, headers=KEY),
        {("pointer", "")},
    ),
    "lowercase currency": (
        lambda c: c.post("/transfer", json={**GOOD_TRANSFER, "currency": "usd"}, headers=KEY),
        {("pointer", "/currency")},
    ),
    "malformed json": (
        lambda c: c.post(
            "/transfer",
            content=b'{"from_account_id": ',
            headers={**KEY, "Content-Type": "application/json"},
        ),
        {("pointer", "")},
    ),
    "body is not an object": (
        lambda c: c.post("/transfer", json=[1], headers=KEY),
        {("pointer", "")},
    ),
    "empty idempotency key": (
        lambda c: c.post("/transfer", json=GOOD_TRANSFER, headers={"Idempotency-Key": ""}),
        {("parameter", "Idempotency-Key")},
    ),
    "path id of zero": (lambda c: c.get("/accounts/0/entries"), {("parameter", "account_id")}),
    "path id that is text": (
        lambda c: c.get("/accounts/abc/entries"),
        {("parameter", "account_id")},
    ),
    "limit above the maximum": (
        lambda c: c.get("/accounts/1/entries?limit=500"),
        {("parameter", "limit")},
    ),
}


@pytest.mark.parametrize("case", sorted(VALIDATION_CASES))
def test_a_validation_error_is_a_400_problem_with_an_errors_list(client, caplog, case) -> None:
    caplog.set_level(logging.DEBUG)
    send, expected = VALIDATION_CASES[case]

    response = send(client)
    body = problem(response)

    assert response.status_code == 400, "validation errors are 400; 422 means insufficient funds"
    assert body["status"] == 400
    assert body["type"] == "urn:ledger:error:invalid-request"
    assert body["title"] == "Invalid request"
    assert set(body) == STANDARD_MEMBERS | {"errors"}
    assert body["instance"] == f"urn:uuid:{request_id(response)}"
    for item in body["errors"]:
        assert item["detail"]
        assert len(set(item) - {"detail"}) == 1
    locators = {(k, v) for item in body["errors"] for k, v in item.items() if k != "detail"}
    assert locators == expected
    assert "secret-value" not in response.text, "the client's input was echoed back"
    assert not at_least(caplog, logging.ERROR)


# --- HTTP errors from Starlette or a route -> problem ---------------------------------------------

HTTP_CASES = {
    "unknown route": (lambda c: c.get("/no-such-route"), 404, "Not Found"),
    "wrong method": (lambda c: c.post("/ok"), 405, "Method Not Allowed"),
    "HTTPException raised by a route": (lambda c: c.get("/teapot"), 418, "short and stout"),
}


@pytest.mark.parametrize("case", sorted(HTTP_CASES))
def test_an_http_error_is_a_problem_with_the_status_phrase_as_its_title(
    client, caplog, case
) -> None:
    caplog.set_level(logging.DEBUG)
    send, status, detail = HTTP_CASES[case]

    response = send(client)
    body = problem(response)

    assert response.status_code == status
    assert body["status"] == status
    assert body["type"] == "about:blank"
    assert body["title"] == HTTPStatus(status).phrase
    assert body["detail"] == detail
    assert set(body) == STANDARD_MEMBERS
    assert body["instance"] == f"urn:uuid:{request_id(response)}"
    assert not at_least(caplog, logging.ERROR)


@pytest.mark.parametrize("status", [204, 304])
def test_a_status_that_cannot_carry_a_body_is_sent_with_none(client, status) -> None:
    """HTTP forbids a body on 1xx, 204, 205 and 304. TestClient would not object to one, so the
    test checks the bytes. A real server can fail on it."""
    response = client.get(f"/http-error/{status}")
    assert response.status_code == status
    assert response.content == b""


@pytest.mark.parametrize("status", [499, 599])
def test_a_status_code_python_has_no_name_for_still_gets_a_problem(client, status) -> None:
    response = client.get(f"/http-error/{status}")
    body = problem(response)
    assert response.status_code == status
    assert body["status"] == status
    assert body["type"] == "about:blank"
    assert body["title"], "the title falls back to a generic one"
    assert body["detail"] == "raised by a route"
    assert body["instance"] == f"urn:uuid:{request_id(response)}"


def test_a_detail_that_is_not_a_string_comes_back_as_a_string(client) -> None:
    response = client.get("/http-error-with-a-dict")
    body = problem(response)
    assert response.status_code == 400
    assert isinstance(body["detail"], str)
    assert "because" in body["detail"]


def test_a_405_keeps_its_allow_header(client) -> None:
    response = client.post("/ok")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]


# --- the request id -----------------------------------------------------------------------------


def test_every_response_has_a_fresh_request_id_and_success_stays_plain_json(client) -> None:
    first, second = client.get("/ok"), client.get("/ok")
    assert request_id(first) != request_id(second)
    assert first.headers["content-type"] == "application/json"
    assert first.json() == {"ok": True}


def test_a_route_that_does_not_exist_still_gets_a_request_id(client) -> None:
    response = client.get("/no-such-route")
    assert response.status_code == 404
    request_id(response)


def test_a_client_cannot_choose_the_request_id(client) -> None:
    """Ids are server-generated, so a client cannot put text of its choosing into the logs.
    Honoring a client-supplied id is what a gateway or load balancer in front of the service would
    do; there is none here."""
    response = client.get("/ok", headers={"X-Request-ID": "chosen-by-the-client"})
    assert request_id(response) != "chosen-by-the-client"


def test_log_records_made_during_a_request_carry_its_id(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    response = client.get("/log")
    records = [r for r in caplog.records if r.name == "ledger.test"]
    assert len(records) == 1
    assert records[0].request_id == request_id(response)
