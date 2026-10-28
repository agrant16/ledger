"""`Retry-After` from `ProblemSpec` (ledger/api/rest/problems.py, openapi.py; #178). No database.

The error handler and the OpenAPI document both have to know whether an error's response carries
`Retry-After`. Today each decides separately (`isinstance(exc, ServiceBusyError)` in the handler,
`status == 503` in `problem_responses`), and they agree only because no route documents a
`NotReadyError`. The spec becomes the single source of truth.

Contract these tests expect:

- `ProblemSpec` has a `retry_after: bool = False` field; in `PROBLEMS` only `ServiceBusyError`
  sets it.
- `ledger_error_handler` sends `Retry-After` exactly when the error's spec has `retry_after`.
- `problem_responses` declares the `Retry-After` header on a status entry exactly when one of the
  errors in that entry has a spec with `retry_after`, not for every 503. A 503 entry that mixes a
  busy and a not-ready error declares it, because one of its errors sends it.

No behavior change: runtime responses and the current OpenAPI document stay the same. These tests
defend no correctness rule; they keep the documentation accurate.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.api.rest import openapi
from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.problems import PROBLEMS, problem_for
from ledger.errors import LedgerError, NotReadyError, ServiceBusyError
from tests.unit.api.api_cases import EXAMPLES

ERRORS_503 = [error for error, spec in PROBLEMS.items() if spec.status == 503]


def _raise_and_get_response(error_class: type[LedgerError]):
    application = FastAPI()
    install_error_handling(application)

    @application.get("/boom")
    def boom() -> None:
        raise EXAMPLES[error_class]()

    return TestClient(application, raise_server_exceptions=False).get("/boom")


def test_only_service_busy_has_retry_after_in_the_table() -> None:
    assert {error for error, spec in PROBLEMS.items() if spec.retry_after} == {ServiceBusyError}


def test_a_spec_has_no_retry_after_unless_it_says_so() -> None:
    assert problem_for(NotReadyError).retry_after is False


def test_problem_responses_declares_retry_after_on_the_503_of_a_busy_error() -> None:
    responses = openapi.problem_responses(ServiceBusyError)
    assert "Retry-After" in responses[503]["headers"]


def test_problem_responses_declares_no_headers_on_the_503_of_a_not_ready_error() -> None:
    responses = openapi.problem_responses(NotReadyError)
    assert "headers" not in responses[503]


def test_a_503_entry_mixing_busy_and_not_ready_declares_retry_after() -> None:
    responses = openapi.problem_responses(ServiceBusyError, NotReadyError)
    assert "Retry-After" in responses[503]["headers"]
    assert "urn:ledger:error:service-busy" in responses[503]["description"]
    assert "urn:ledger:error:not-ready" in responses[503]["description"]


@pytest.mark.parametrize("error_class", ERRORS_503, ids=lambda error: error.__name__)
def test_the_handlers_retry_after_matches_what_openapi_declares(
    error_class: type[LedgerError],
) -> None:
    response = _raise_and_get_response(error_class)
    declared = "Retry-After" in openapi.problem_responses(error_class)[503].get("headers", {})

    assert response.status_code == 503
    assert ("Retry-After" in response.headers) is declared
