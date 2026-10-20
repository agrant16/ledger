"""The overall concurrency cap through the real app (docs/design.md, Stack decisions and Error
behavior; ADRs 0004, 0009).

The service rejects requests above CONCURRENCY_CAP in flight with a 503 `service-busy` problem and
a Retry-After header, answered by a middleware before any transaction starts. The middleware sits
inside the request-id middleware, so the 503 carries a request id like every other response.

The cap is 2. The test first puts two requests in flight and holds them inside the route with a
barrier (the test thread is the third party), so the next request provably arrives at the cap. The
held requests are `GET /accounts/1/balance` calls whose service function waits to be released; the
function then runs for real, so a released request ends as an ordinary 404. Every wait has its own
timeout, so a hang fails the test and not the suite.

The cap counts every HTTP request, `/health` included.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.api.rest.error_handlers import RETRY_AFTER_SECONDS
from ledger.api.rest.routers import accounts as accounts_router
from tests.db_helpers import reset_database

pytestmark = pytest.mark.usefixtures("migrated_database")

CAP = 2
WAIT_SECONDS = 10
READ = "/accounts/1/balance"


@pytest.fixture(autouse=True)
def empty_database(owner_conn: psycopg.Connection) -> Iterator[None]:
    """No leftover rows, so the no-write test cannot be fooled by an earlier test's account."""
    reset_database(owner_conn)
    yield
    reset_database(owner_conn)


class Hold:
    """Makes the first CAP calls of `get_balance` wait at a barrier, then for a release."""

    def __init__(self, original: Callable) -> None:
        self.original = original
        self.barrier = threading.Barrier(CAP + 1, timeout=WAIT_SECONDS)
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def get_balance(self, conn, account_id):
        with self._lock:
            self.calls += 1
            held = self.calls <= CAP
        if held:
            self.barrier.wait()
            assert self.release.wait(WAIT_SECONDS), "the test never released the held request"
        return self.original(conn, account_id)


@dataclass
class Saturated:
    client: TestClient
    hold: Hold
    held: list[Future]


@pytest.fixture
def saturated(
    fresh_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Saturated]:
    """The real app with CAP requests in flight, all held inside the route."""
    app = fresh_app(CONCURRENCY_CAP=str(CAP))
    hold = Hold(accounts_router.get_balance)
    monkeypatch.setattr(accounts_router, "get_balance", hold.get_balance)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        ThreadPoolExecutor(CAP) as executor,
    ):
        held = [executor.submit(client.get, READ) for _ in range(CAP)]
        try:
            hold.barrier.wait()  # fails the test if a held request never got in
            yield Saturated(client, hold, held)
        finally:
            hold.release.set()


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", READ, None),
        ("POST", "/accounts", {"account_name": "customer:capped", "currency": "USD"}),
        ("GET", "/health", None),
    ],
    ids=["read", "write", "health"],
)
def test_the_request_over_the_cap_gets_a_503_with_retry_after(
    saturated: Saturated,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    caplog.set_level(logging.DEBUG)

    response = saturated.client.request(method, path, json=body)

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
    assert len(warnings) == 1, "a rejection is logged once, at WARNING"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_rejected_request_never_reaches_the_route_and_the_held_ones_finish(
    saturated: Saturated,
) -> None:
    rejected = saturated.client.get(READ)
    assert rejected.status_code == 503
    assert saturated.hold.calls == CAP, "the rejected request must not reach the route"

    saturated.hold.release.set()
    for future in saturated.held:
        response = future.result(timeout=WAIT_SECONDS)
        assert response.status_code == 404  # no such account: an ordinary answer, not a 503
        assert response.json()["type"] == "urn:ledger:error:unknown-account"


def test_a_rejected_write_creates_nothing(
    saturated: Saturated, owner_conn: psycopg.Connection
) -> None:
    response = saturated.client.post(
        "/accounts", json={"account_name": "customer:capped", "currency": "USD"}
    )
    assert response.status_code == 503

    row = owner_conn.execute(
        "SELECT count(*) FROM accounts WHERE account_name = %s", ("customer:capped",)
    ).fetchone()
    assert row == (0,)


def test_the_cap_is_lifted_once_the_held_requests_finish(saturated: Saturated) -> None:
    assert saturated.client.get(READ).status_code == 503
    saturated.hold.release.set()
    for future in saturated.held:
        future.result(timeout=WAIT_SECONDS)

    # Far more requests than the cap, one after another: none of them is a 503.
    statuses = [saturated.client.get(READ).status_code for _ in range(CAP * 3)]

    assert statuses == [404] * (CAP * 3)
