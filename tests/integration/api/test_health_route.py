"""GET /health (ledger/api/rest/routers/health.py; docs/design.md, API; ADR 0013).

Contract these tests expect:

- ledger/api/rest/routers/health.py has an `APIRouter` called `router` with the route.
- The route is a synchronous function that calls `check_database(request.app.state.pool)`, imported
  by that name from ledger.infra.db so a test can replace it. It runs no SQL itself.
- 200 with {"status": "ok"} when the check returns, with the request id header like every other
  response. It is left out of the OpenAPI document: it is for the platform, not for clients.
- It is a readiness check. When the check raises a ServiceBusyError (a pool wait timeout), the
  response is the same 503 `service-busy` problem with Retry-After as on any other route, logged at
  WARNING. When it raises anything else, the failure is not hidden: the response is the generic 500
  problem and the exception is logged once.

The real app answering on /health is tested in test_real_app_error_handling.py and the compose
smoke job.
"""

import logging

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.api.rest.error_handlers import RETRY_AFTER_SECONDS, install_error_handling
from ledger.api.rest.routers.health import router
from ledger.errors import ServiceBusyError


@pytest.fixture
def app(app_pool: ConnectionPool) -> FastAPI:
    application = FastAPI()
    install_error_handling(application)
    application.include_router(router)
    application.state.pool = app_pool
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_health_is_ok_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_health_asks_the_check_about_the_apps_pool(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = []
    monkeypatch.setattr(
        "ledger.api.rest.routers.health.check_database", lambda pool: asked.append(pool)
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert asked == [app.state.pool]


def test_a_failing_check_is_a_logged_500_and_not_a_healthy_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = psycopg.OperationalError("connection refused")

    def refuse(pool):
        raise failure

    monkeypatch.setattr("ledger.api.rest.routers.health.check_database", refuse)
    caplog.set_level(logging.DEBUG)

    response = client.get("/health")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "about:blank"
    assert "refused" not in response.text
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert [r.exc_info[1] for r in errors] == [failure]


def test_a_busy_pool_is_a_503_with_retry_after_and_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def busy(pool):
        raise ServiceBusyError("Connection timeout")

    monkeypatch.setattr("ledger.api.rest.routers.health.check_database", busy)
    caplog.set_level(logging.DEBUG)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    assert response.json()["type"] == "urn:ledger:error:service-busy"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_health_is_not_in_the_openapi_document(client: TestClient) -> None:
    assert "/health" not in client.get("/openapi.json").json()["paths"]


def test_only_get_is_allowed(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    assert response.headers["content-type"].startswith("application/problem+json")
