"""`/health` runs on its own thread limiter (issue #155; docs/design.md, Stack decisions; ADR 0004).

The concurrency cap does not count `/health`, but a synchronous route still runs on a worker
thread. If the check used anyio's default limiter (HANDLER_THREAD_LIMIT tokens, shared with every
traffic handler), a burst of probes could take every handler thread, and with traffic holding them
all a probe would wait for a thread with no timeout, which Kubernetes reads as "not ready".

Contract these tests expect:

- Settings has `health_concurrency` (environment variable `HEALTH_CONCURRENCY`, a positive integer,
  default 2). It is validated in tests/unit/test_config.py and logged at startup in
  test_startup_settings_line.py.
- The lifespan puts an `anyio.CapacityLimiter` of that size in `app.state.health_limiter`. It is a
  different limiter from the default one.
- The `/health` route is `async` and runs `check_readiness` with
  `anyio.to_thread.run_sync(..., limiter=request.app.state.health_limiter)`, so the check never
  takes a token from the default limiter and at most `health_concurrency` checks run at once.
  `check_readiness` is still imported into the route's module by that name so a test can replace it.

No correctness rule is involved: nothing here reads or writes ledger data.
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from anyio import CapacityLimiter, to_thread
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.infra import readiness
from tests.db_helpers import commit_accounts, commit_transfer, reset_database

pytestmark = pytest.mark.usefixtures("migrated_database")

# Long enough for a loaded CI runner, short enough that a hung request fails the test promptly.
WAIT_SECONDS = 10
UNKNOWN_ACCOUNT = 999_999_999
FUNDED = 1000  # what alice holds when the transfer test starts
AMOUNT = 300


def ready_result() -> readiness.ReadinessResult:
    return readiness.ReadinessResult(failed_check=None, cause=None)


class SlowCheck:
    """A `check_readiness` stand-in that blocks until released and records how many run at once."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0

    def __call__(self, app_url: str, timeout_seconds: float) -> readiness.ReadinessResult:
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
        self.entered.release()
        try:
            assert self.release.wait(WAIT_SECONDS), "the test never released the check"
        finally:
            with self._lock:
                self._running -= 1
        return ready_result()

    def wait_until_entered(self, count: int) -> None:
        for _ in range(count):
            assert self.entered.acquire(timeout=WAIT_SECONDS), "a check never started"


@pytest.fixture
def slow_check(monkeypatch: pytest.MonkeyPatch) -> Iterator[SlowCheck]:
    check = SlowCheck()
    monkeypatch.setattr("ledger.api.rest.routers.health.check_readiness", check)
    yield check
    check.release.set()  # never leave a worker thread waiting if a test failed early


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=16) as pool:
        yield pool


def started(fresh_app: Callable[..., FastAPI], **env: str) -> TestClient:
    return TestClient(fresh_app(**env), raise_server_exceptions=False)


async def hold_every_default_token() -> list[object]:
    limiter = to_thread.current_default_thread_limiter()
    borrowers = [object() for _ in range(int(limiter.total_tokens))]
    for borrower in borrowers:
        await limiter.acquire_on_behalf_of(borrower)
    return borrowers


async def release_default_tokens(borrowers: list[object]) -> None:
    limiter = to_thread.current_default_thread_limiter()
    for borrower in borrowers:
        limiter.release_on_behalf_of(borrower)


async def health_limiter_of(app: FastAPI) -> CapacityLimiter:
    return app.state.health_limiter


def wait_until_queued(limiter: CapacityLimiter, count: int) -> None:
    """Waits until `count` requests are queued on the limiter, polling the limiter itself."""
    deadline = time.monotonic() + WAIT_SECONDS
    while limiter.statistics().tasks_waiting < count:
        assert time.monotonic() < deadline, "the other probes never reached the limiter"
        time.sleep(0.01)


def test_startup_creates_a_health_limiter_of_the_configured_size_apart_from_the_default_one(
    fresh_app: Callable[..., FastAPI],
) -> None:
    app = fresh_app(HEALTH_CONCURRENCY="3", HANDLER_THREAD_LIMIT="7")

    with TestClient(app) as client:
        limiter = client.portal.call(health_limiter_of, app)
        default = client.portal.call(to_thread.current_default_thread_limiter)

    assert isinstance(limiter, CapacityLimiter)
    assert limiter.total_tokens == 3
    assert limiter is not default
    assert default.total_tokens == 7


def test_the_health_limiter_defaults_to_2(
    fresh_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HEALTH_CONCURRENCY", raising=False)
    app = fresh_app()

    with TestClient(app) as client:
        limiter = client.portal.call(health_limiter_of, app)

    assert limiter.total_tokens == 2


def test_health_is_ready_while_every_handler_thread_is_taken(
    fresh_app: Callable[..., FastAPI], executor: ThreadPoolExecutor
) -> None:
    """The probe must not queue behind traffic for a handler thread."""
    with started(fresh_app, HANDLER_THREAD_LIMIT="2") as client:
        borrowers = client.portal.call(hold_every_default_token)
        try:
            response = executor.submit(client.get, "/health").result(timeout=WAIT_SECONDS)
        finally:
            client.portal.call(release_default_tokens, borrowers)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "pool": "ok"}


def test_a_burst_of_health_checks_never_runs_more_than_the_limit_at_once(
    fresh_app: Callable[..., FastAPI], executor: ThreadPoolExecutor, slow_check: SlowCheck
) -> None:
    app = fresh_app(HEALTH_CONCURRENCY="2")
    with TestClient(app, raise_server_exceptions=False) as client:
        limiter = client.portal.call(health_limiter_of, app)
        pending = [executor.submit(client.get, "/health") for _ in range(8)]
        slow_check.wait_until_entered(2)
        wait_until_queued(limiter, 6)  # the other 6 are waiting for a slot, none has started
        assert limiter.statistics().borrowed_tokens == 2
        assert slow_check.peak == 2
        slow_check.release.set()
        responses = [future.result(timeout=WAIT_SECONDS) for future in pending]

    assert [r.status_code for r in responses] == [200] * 8
    assert slow_check.peak == 2


def test_traffic_is_served_while_every_health_slot_is_busy(
    fresh_app: Callable[..., FastAPI], executor: ThreadPoolExecutor, slow_check: SlowCheck
) -> None:
    """Health checks must not use the handler threads, even when the handler limit is as low as
    the health limit. An unknown account is a 404 that still needs a handler thread and a pooled
    connection."""
    with started(fresh_app, HANDLER_THREAD_LIMIT="2", HEALTH_CONCURRENCY="2") as client:
        probes = [executor.submit(client.get, "/health") for _ in range(4)]
        slow_check.wait_until_entered(2)

        traffic = executor.submit(client.get, f"/accounts/{UNKNOWN_ACCOUNT}/balance")
        response = traffic.result(timeout=WAIT_SECONDS)

        slow_check.release.set()
        for probe in probes:
            probe.result(timeout=WAIT_SECONDS)

    assert response.status_code == 404


def test_a_transfer_completes_while_every_health_slot_is_busy(
    fresh_app: Callable[..., FastAPI],
    executor: ThreadPoolExecutor,
    slow_check: SlowCheck,
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
) -> None:
    """A write needs a handler thread, a pooled connection, row locks and the key claim. It must
    finish while the probes hold every health slot and more are queued."""
    reset_database(owner_conn)
    funding, alice, bob = commit_accounts(app_pool)
    commit_transfer(app_pool, "fund-alice", funding.id, alice.id, FUNDED)
    body = {
        "from_account_id": alice.id,
        "to_account_id": bob.id,
        "amount_minor": AMOUNT,
        "currency": "USD",
    }
    try:
        with started(fresh_app, HANDLER_THREAD_LIMIT="2", HEALTH_CONCURRENCY="2") as client:
            probes = [executor.submit(client.get, "/health") for _ in range(4)]
            slow_check.wait_until_entered(2)

            response = executor.submit(
                client.post, "/transfers", json=body, headers={"Idempotency-Key": "during-probes"}
            ).result(timeout=WAIT_SECONDS)

            slow_check.release.set()
            for probe in probes:
                probe.result(timeout=WAIT_SECONDS)

        assert response.status_code == 201
        balances = owner_conn.execute(
            "SELECT account_id, balance_minor FROM balances WHERE account_id IN (%s, %s)"
            " ORDER BY account_id",
            (alice.id, bob.id),
        ).fetchall()
        assert dict(balances) == {alice.id: FUNDED - AMOUNT, bob.id: AMOUNT}
    finally:
        reset_database(owner_conn)


def test_a_check_that_fails_gives_its_slot_back(
    fresh_app: Callable[..., FastAPI],
    executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the check (a 500) must not leak a slot: after more failures than there are slots,
    the next probe still gets one."""

    def broken(app_url: str, timeout_seconds: float) -> readiness.ReadinessResult:
        raise RuntimeError("a bug in the check")

    monkeypatch.setattr("ledger.api.rest.routers.health.check_readiness", broken)
    with started(fresh_app, HEALTH_CONCURRENCY="1") as client:
        failures = [client.get("/health") for _ in range(3)]
        monkeypatch.setattr(
            "ledger.api.rest.routers.health.check_readiness",
            lambda app_url, timeout_seconds: ready_result(),
        )
        response = executor.submit(client.get, "/health").result(timeout=WAIT_SECONDS)

    assert [r.status_code for r in failures] == [500] * 3
    assert response.status_code == 200
