"""The real app (built by ledger.main.create_app) has the error handling installed.

unit/api/test_problem_responses.py builds a throwaway app, so it cannot notice if
`install_error_handling` is never called on the real one. These tests look at the real app: its
handlers are ours and not FastAPI's defaults, an unknown route comes back as a problem with a
request id, and /health, which the compose smoke job calls, still works and now carries a request
id too.
"""

import json
import uuid

import pytest
from fastapi import exception_handlers as defaults
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledger.errors import LedgerError
from ledger.main import create_app

pytestmark = pytest.mark.usefixtures("migrated_database")


def test_the_real_app_uses_its_own_handlers() -> None:
    handlers = create_app().exception_handlers
    assert LedgerError in handlers
    assert Exception in handlers
    assert handlers[RequestValidationError] is not defaults.request_validation_exception_handler
    assert handlers[StarletteHTTPException] is not defaults.http_exception_handler


def test_an_unknown_route_on_the_real_app_is_a_problem_with_a_request_id() -> None:
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.get("/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = json.loads(response.content)
    assert (body["type"], body["title"], body["status"]) == ("about:blank", "Not Found", 404)
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"


def test_health_still_works_and_carries_a_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "pool": "ok"}
    uuid.UUID(response.headers["X-Request-ID"])
