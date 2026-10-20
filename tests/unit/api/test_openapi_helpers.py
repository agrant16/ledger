"""The OpenAPI helpers (ledger/api/rest/openapi.py; issue #107). No database.

Contract these tests expect:

    problem_responses(*error_classes: type[LedgerError]) -> dict[int, dict]

Builds the `responses=` argument of a route from the error table, so a documented status, type and
title cannot drift from what the handlers send:
- one entry per distinct status of the given classes, plus the generic 500 every route can return;
  errors that share a status (the two 400s of a transfer) share one entry whose description names
  each `type` URN;
- every entry is `application/problem+json` with the `ProblemDetails` schema;
- a 503 entry also declares a `Retry-After` header.

    install_openapi(app: FastAPI) -> None

Replaces the app's OpenAPI generation so the document drops the `422` / `HTTPValidationError` that
FastAPI adds to every route with parameters, because validation errors are 400 here and 422 means
insufficient funds. A 422 a route documents itself is kept.
"""

from fastapi import FastAPI

from ledger.api.rest import openapi
from ledger.api.rest.problems import problem_for
from ledger.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidRequestError,
    ServiceBusyError,
    UnknownAccountError,
)


def test_each_distinct_status_gets_one_entry_and_a_500_is_always_added() -> None:
    responses = openapi.problem_responses(UnknownAccountError, InsufficientFundsError)
    assert set(responses) == {404, 422, 500}


def test_errors_sharing_a_status_share_one_entry_naming_every_type() -> None:
    responses = openapi.problem_responses(InvalidRequestError, CurrencyMismatchError)
    description = responses[400]["description"]
    assert "urn:ledger:error:invalid-request" in description
    assert "urn:ledger:error:currency-mismatch" in description


def test_every_entry_is_problem_json_with_the_problem_schema() -> None:
    responses = openapi.problem_responses(UnknownAccountError, ServiceBusyError)
    for entry in responses.values():
        assert set(entry["content"]) == {"application/problem+json"}


def test_a_503_declares_retry_after_and_other_entries_do_not() -> None:
    responses = openapi.problem_responses(UnknownAccountError, ServiceBusyError)
    assert "Retry-After" in responses[503]["headers"]
    assert "headers" not in responses[404]


def test_the_response_is_documented_from_the_error_table() -> None:
    (status,) = {s for s in openapi.problem_responses(UnknownAccountError) if s != 500}
    assert status == problem_for(UnknownAccountError).status


def test_install_openapi_removes_fastapis_422_and_keeps_a_documented_one() -> None:
    app = FastAPI()

    @app.get("/plain")
    def plain(page: int = 0) -> dict:
        return {}

    @app.get("/funds", responses=openapi.problem_responses(InsufficientFundsError))
    def funds(page: int = 0) -> dict:
        return {}

    assert "422" in app.openapi()["paths"]["/plain"]["get"]["responses"]  # FastAPI's own
    app.openapi_schema = None
    openapi.install_openapi(app)
    document = app.openapi()

    assert "422" not in document["paths"]["/plain"]["get"]["responses"]
    assert "422" in document["paths"]["/funds"]["get"]["responses"]
    assert "HTTPValidationError" not in str(document)
