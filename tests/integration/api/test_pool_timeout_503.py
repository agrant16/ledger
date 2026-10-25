"""A pool wait timeout is a 503 (docs/design.md, Error behavior and Error model; ADRs 0004, 0009).

A request that cannot get a database connection within the configured wait (POOL_TIMEOUT_SECONDS)
is a busy service, not a bug: the response is a 503 `service-busy` problem with a Retry-After
header, and the cause is logged at WARNING. No transaction started, so retrying is safe and nothing
was written.

These tests use the real app (built by ledger.main.create_app) with a pool of one connection,
which the test holds to exhaust it, so the wiring from the pool to the response is covered end to
end. The connection is held by the test, never by a request, so no request takes a second
connection while holding one.
"""

import logging
import time
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

import ledger.main
from ledger.api.rest.error_handlers import RETRY_AFTER_SECONDS

pytestmark = pytest.mark.usefixtures("migrated_database")

READ = ("GET", "/accounts/1/balance", None)
WRITE_BODY = {"account_name": "customer:busy", "currency": "USD"}
WRITE = ("POST", "/accounts", WRITE_BODY)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("POOL_SIZE", "1")
    monkeypatch.setenv("POOL_TIMEOUT_SECONDS", "0.3")
    with TestClient(ledger.main.create_app(), raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("method, path, body", [READ, WRITE], ids=["read", "write"])
def test_an_exhausted_pool_gives_a_503_with_retry_after(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    caplog.set_level(logging.DEBUG)

    with client.app.state.pool.connection():  # the only connection is in use
        response = client.request(method, path, json=body)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    problem = response.json()
    assert (problem["type"], problem["title"], problem["status"]) == (
        "urn:ledger:error:service-busy",
        "Service busy",
        503,
    )
    assert problem["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "get a connection" in warnings[0].getMessage()  # the pool's own timeout message
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_exhausted_pool_does_not_make_health_fail(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The readiness check uses its own connection (issue #155), so a pool with every connection in
    use is reported as saturated and is still ready."""
    caplog.set_level(logging.DEBUG)

    with client.app.state.pool.connection():  # the only connection is in use
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "pool": "saturated"}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_request_waits_the_configured_time_before_the_503(client: TestClient) -> None:
    with client.app.state.pool.connection():
        started = time.monotonic()
        response = client.get(READ[1])
        waited = time.monotonic() - started

    assert response.status_code == 503
    assert 0.25 <= waited < 2


def test_a_503_writes_nothing(client: TestClient, owner_conn: psycopg.Connection) -> None:
    with client.app.state.pool.connection():
        response = client.post(WRITE[1], json=WRITE_BODY)
    assert response.status_code == 503

    row = owner_conn.execute(
        "SELECT count(*) FROM accounts WHERE account_name = %s", (WRITE_BODY["account_name"],)
    ).fetchone()
    assert row == (0,)


def test_the_pool_recovers_once_the_connection_is_free(client: TestClient) -> None:
    with client.app.state.pool.connection():
        assert client.get(READ[1]).status_code == 503

    # A free connection: the request runs and finds no such account (404), not a 503 or a 500.
    response = client.get(READ[1])
    assert response.status_code == 404
    assert response.json()["type"] == "urn:ledger:error:unknown-account"
