"""NotReadyError (ledger/errors.py, ledger/api/rest/problems.py; docs/design.md, Error model).

The readiness check (issue #155) answers 503 when the service cannot do its job: the database is
unreachable, read-only, or missing a table or grant. It is a `LedgerError` with its own row in the
API layer's table, a different problem type from `ServiceBusyError` because a database that is
down is not a busy service, and unlike a busy answer it carries no Retry-After: a probe ignores it,
and a client has no reason to retry soon.

Contract these tests expect:

- `NotReadyError(LedgerError)` in ledger/errors.py.
- Its row in the table: status 503, type `urn:ledger:error:not-ready`, title "Not ready".
- The LedgerError handler adds Retry-After and the "busy" warning only for `ServiceBusyError`, not
  for every 503. The warning for a `NotReadyError` is still one WARNING line, and it carries the
  cause.

When the error is added to ledger/errors.py, add it to EXPECTED and EXAMPLES in
tests/unit/api/api_cases.py too, so the shared table tests cover it, and change the Retry-After
assertion in tests/unit/api/test_problem_responses.py to "only ServiceBusyError sends it".
"""

import logging

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.api.rest import problems as table
from ledger.api.rest.error_handlers import install_error_handling
from ledger.errors import LedgerError, NotReadyError, ServiceBusyError


def test_it_is_a_ledger_error_so_it_is_an_expected_failure_and_not_a_bug() -> None:
    assert issubclass(NotReadyError, LedgerError)


def test_it_has_its_own_row_in_the_table() -> None:
    spec = table.problem_for(NotReadyError)

    assert (spec.status, spec.type, spec.title) == (
        503,
        "urn:ledger:error:not-ready",
        "Not ready",
    )


def test_it_is_a_different_problem_from_a_busy_service() -> None:
    assert table.problem_for(NotReadyError).type != table.problem_for(ServiceBusyError).type


def test_the_response_has_no_retry_after_and_one_warning_with_the_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = FastAPI()
    install_error_handling(application)
    cause = psycopg.OperationalError("connection refused")

    @application.get("/not-ready")
    def raise_not_ready() -> None:
        raise NotReadyError("the database check failed") from cause

    caplog.set_level(logging.DEBUG)

    response = TestClient(application, raise_server_exceptions=False).get("/not-ready")

    assert response.status_code == 503
    assert response.json()["type"] == "urn:ledger:error:not-ready"
    assert "Retry-After" not in response.headers
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "connection refused" in warnings[0].getMessage()
    assert "busy" not in warnings[0].getMessage().lower()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
