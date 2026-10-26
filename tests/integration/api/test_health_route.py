"""GET /health (ledger/api/rest/routers/health.py; docs/design.md, API; ADR 0004, ADR 0013).

Contract these tests expect:

- ledger/api/rest/routers/health.py has an `APIRouter` called `router` with the route.
- The route is an `async` function that runs `check_readiness(settings.app_url,
  settings.health_timeout_seconds)` on the limiter in `app.state.health_limiter` (see
  test_health_thread_isolation.py) and calls `pool_state(pool)`, both imported by those names from
  ledger.infra.readiness so a test can replace them, with the settings in `app.state.settings` and
  the pool in `app.state.pool`. It runs no SQL itself.
- It is a readiness check (issue #155). When the check is ready the response is 200 with
  {"status": "ok", "pool": "ok"}, or "pool": "saturated" when every pool connection is in use: a
  busy pool is information, not a failure. The request id header is there like on every other
  response. The route is left out of the OpenAPI document: it is for the platform, not for clients.
- When the check is not ready the route raises `NotReadyError(...) from result.cause`: a 503
  `not-ready` problem with no Retry-After (a probe ignores it) and a generic detail with nothing
  from the database. It is logged once at WARNING, naming the failed check and carrying the cause.
- When the check itself raises, that is a bug: the response is the generic 500 problem and the
  exception is logged once.

The real check against the database is tested in tests/integration/db/test_check_readiness.py.
The real app answering on /health is tested in test_real_app_error_handling.py and the compose
smoke job.
"""

import logging
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.routers.health import router
from ledger.infra import readiness
from ledger.infra.config import Settings


@pytest.fixture
def pool_of_one(settings: Settings) -> Iterator[ConnectionPool]:
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=0.5, open=True) as pool:
        pool.wait()
        yield pool


@asynccontextmanager
async def health_limiter_lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Like the real lifespan, the limiter is made inside the running event loop.
    application.state.health_limiter = CapacityLimiter(
        application.state.settings.health_concurrency
    )
    yield


def build_app(settings: Settings, pool: ConnectionPool) -> FastAPI:
    application = FastAPI(lifespan=health_limiter_lifespan)
    install_error_handling(application)
    application.include_router(router)
    application.state.pool = pool
    application.state.settings = settings
    return application


@pytest.fixture
def app(settings: Settings, app_pool: ConnectionPool) -> FastAPI:
    return build_app(settings, app_pool)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


def test_health_is_ok_when_the_database_is_ready(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok", "pool": "ok"}
    assert response.headers["X-Request-ID"]


def test_health_asks_the_check_about_the_apps_settings_and_pool(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = []
    asked_about = []

    def check(app_url, timeout_seconds):
        checked.append((app_url, timeout_seconds))
        return readiness.ReadinessResult(failed_check=None, cause=None)

    def state(pool):
        asked_about.append(pool)
        return "ok"

    monkeypatch.setattr("ledger.api.rest.routers.health.check_readiness", check)
    monkeypatch.setattr("ledger.api.rest.routers.health.pool_state", state)

    response = client.get("/health")

    assert response.status_code == 200
    settings = app.state.settings
    assert checked == [(settings.app_url, settings.health_timeout_seconds)]
    assert asked_about == [app.state.pool]


def test_a_saturated_pool_is_still_ready_and_is_reported(
    settings: Settings, pool_of_one: ConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    """The check does not use the request pool, so a pool with every connection in use does not make
    the service unready: the concurrency cap and Retry-After deal with that, request by request."""
    caplog.set_level(logging.DEBUG)

    with TestClient(build_app(settings, pool_of_one), raise_server_exceptions=False) as client:
        with pool_of_one.connection():  # the only connection is in use
            response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "pool": "saturated"}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("failed_check", ["database", "writable", "tables"])
def test_a_check_that_is_not_ready_is_a_503_not_ready_problem_without_retry_after(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_check: str,
) -> None:
    cause = psycopg.OperationalError('password authentication failed for user "ledger_app"')
    monkeypatch.setattr(
        "ledger.api.rest.routers.health.check_readiness",
        lambda app_url, timeout_seconds: readiness.ReadinessResult(
            failed_check=failed_check, cause=cause
        ),
    )
    caplog.set_level(logging.DEBUG)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "Retry-After" not in response.headers
    problem = response.json()
    assert (problem["type"], problem["title"], problem["status"]) == (
        "urn:ledger:error:not-ready",
        "Not ready",
        503,
    )
    assert problem["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"
    assert isinstance(problem["detail"], str)
    assert "password" not in response.text
    assert "ledger_app" not in response.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "a not-ready answer is logged once, at WARNING"
    assert f"failed_check={failed_check}" in warnings[0].getMessage()
    assert "password authentication failed" in warnings[0].getMessage()  # the cause
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_revoked_grant_is_a_503_not_ready_problem_through_the_real_check(
    client: TestClient, owner_conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """No monkeypatching: the real check runs against the real database."""
    assert client.get("/health").status_code == 200  # the control: it starts ready
    owner_conn.execute("REVOKE INSERT ON entries FROM ledger_app")
    caplog.set_level(logging.DEBUG)
    try:
        response = client.get("/health")
    finally:
        owner_conn.execute("GRANT INSERT ON entries TO ledger_app")

    assert response.status_code == 503
    assert response.json()["type"] == "urn:ledger:error:not-ready"
    assert "Retry-After" not in response.headers
    assert "entries" not in response.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "failed_check=tables" in warnings[0].getMessage()
    assert client.get("/health").status_code == 200  # the grant is back


def test_an_unreachable_database_is_a_503_not_ready_problem_through_the_real_check(
    settings: Settings, app_pool: ConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    parts = urlsplit(settings.app_url)
    userinfo = parts.netloc.rpartition("@")[0]
    dead_url = urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{port}"))
    unreachable = settings.model_copy(update={"app_url": dead_url})
    caplog.set_level(logging.DEBUG)

    with TestClient(build_app(unreachable, app_pool), raise_server_exceptions=False) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["type"] == "urn:ledger:error:not-ready"
    assert "Retry-After" not in response.headers
    assert "127.0.0.1" not in response.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "failed_check=database" in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_bug_in_the_check_is_a_logged_500_and_not_a_not_ready_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError("a bug in the check")

    def broken(app_url, timeout_seconds):
        raise failure

    monkeypatch.setattr("ledger.api.rest.routers.health.check_readiness", broken)
    caplog.set_level(logging.DEBUG)

    response = client.get("/health")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "about:blank"
    assert "a bug" not in response.text
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert [r.exc_info[1] for r in errors] == [failure]


def test_health_is_not_in_the_openapi_document(client: TestClient) -> None:
    assert "/health" not in client.get("/openapi.json").json()["paths"]


def test_only_get_is_allowed(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    assert response.headers["content-type"].startswith("application/problem+json")
