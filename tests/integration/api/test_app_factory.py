"""The app factory loads the settings once (#100; docs/design.md, Stack decisions).

`ledger.main.create_app(settings=None)` loads the settings a single time (or takes the ones it is
given), and that one object feeds everything that depends on them: the concurrency cap middleware,
the pool, the handler thread limit and the startup INFO line. Before the factory the middleware
read the cap when the module was imported and the lifespan read the rest at startup, so the cap in
the log line could differ from the cap that was enforced.

Each test builds an app, then changes the environment, then starts the app. If anything still read
the environment after the build, it would see the second set of values. Which of the two sets the
app uses is the whole point of the tests.

No correctness rule is involved: this is wiring.

`install_error_handling` takes the cap as an argument with a default, so apps built by hand in the
tests (most of them) need no settings.
"""

import inspect
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from anyio import to_thread
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.routers import accounts as accounts_router
from ledger.infra.config import Settings
from ledger.main import app as module_level_app
from ledger.main import create_app

pytestmark = pytest.mark.usefixtures("migrated_database")

PAIR = re.compile(r"(\w+)=(\S+)")
WAIT_SECONDS = 10
READ = "/accounts/1/balance"

# What the tests ask for when the app is built, and what the environment holds afterwards.
BUILT = {
    "POOL_SIZE": "3",
    "POOL_TIMEOUT_SECONDS": "1.5",
    "HANDLER_THREAD_LIMIT": "7",
    "CONCURRENCY_CAP": "1",
}
CHANGED_AFTERWARDS = {
    "POOL_SIZE": "4",
    "POOL_TIMEOUT_SECONDS": "0.5",
    "HANDLER_THREAD_LIMIT": "8",
    "CONCURRENCY_CAP": "6",
}


def set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


async def current_thread_limit() -> float:
    return to_thread.current_default_thread_limiter().total_tokens


def logged_pairs(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    (record,) = [r for r in caplog.records if "concurrency_cap=" in r.getMessage()]
    return dict(PAIR.findall(record.getMessage()))


def test_the_settings_are_read_when_the_app_is_built_and_not_again_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    set_env(monkeypatch, BUILT)
    app = create_app()
    set_env(monkeypatch, CHANGED_AFTERWARDS)

    with TestClient(app) as client:
        limit = client.portal.call(current_thread_limit)
        pool_size = app.state.pool.max_size

    pairs = logged_pairs(caplog)
    assert pairs["pool_size"] == "3"
    assert pairs["pool_timeout_seconds"] == "1.5"
    assert pairs["handler_thread_limit"] == "7"
    assert pairs["concurrency_cap"] == "1"
    assert limit == 7
    assert pool_size == 3


def test_the_cap_that_is_enforced_is_the_cap_that_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bug behind #100. The cap is 1 when the app is built and 6 in the environment when it
    starts. One request is held inside a route, so the next one arrives at the cap: it must be
    rejected, and the log line must say 1."""
    caplog.set_level(logging.INFO)
    set_env(monkeypatch, BUILT)
    app = create_app()
    set_env(monkeypatch, CHANGED_AFTERWARDS)
    entered, release = threading.Event(), threading.Event()
    original = accounts_router.get_balance

    def held_get_balance(conn, account_id):
        entered.set()
        assert release.wait(WAIT_SECONDS), "the test never released the held request"
        return original(conn, account_id)

    monkeypatch.setattr(accounts_router, "get_balance", held_get_balance)

    with (
        TestClient(app, raise_server_exceptions=False) as client,
        ThreadPoolExecutor(1) as executor,
    ):
        held = executor.submit(client.get, READ)
        try:
            assert entered.wait(WAIT_SECONDS), "the held request never reached the route"
            assert client.get("/health").status_code == 503
        finally:
            release.set()
        held.result(timeout=WAIT_SECONDS)

    assert logged_pairs(caplog)["concurrency_cap"] == "1"


def test_given_settings_are_used_and_the_environment_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    set_env(monkeypatch, CHANGED_AFTERWARDS)
    settings = Settings(
        POOL_SIZE=2, POOL_TIMEOUT_SECONDS=0.7, HANDLER_THREAD_LIMIT=5, CONCURRENCY_CAP=9
    )

    app = create_app(settings)

    with TestClient(app) as client:
        limit = client.portal.call(current_thread_limit)
        pool_size = app.state.pool.max_size
    pairs = logged_pairs(caplog)
    assert (pairs["pool_size"], pairs["handler_thread_limit"], pairs["concurrency_cap"]) == (
        "2",
        "5",
        "9",
    )
    assert pairs["pool_timeout_seconds"] == "0.7"
    assert (limit, pool_size) == (5, 2)


def test_two_apps_from_the_factory_keep_their_own_settings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    first = create_app(Settings(HANDLER_THREAD_LIMIT=3))
    second = create_app(Settings(HANDLER_THREAD_LIMIT=9))
    limits = []

    for app in (first, second, first):
        with TestClient(app) as client:
            limits.append(client.portal.call(current_thread_limit))

    assert limits == [3, 9, 3]


def test_the_module_level_app_comes_from_the_factory_and_serves_requests() -> None:
    """`uvicorn ledger.main:app` keeps working: the module still exposes a ready `app`."""
    with TestClient(module_level_app) as client:
        assert client.get("/health").status_code == 200


def test_install_error_handling_takes_a_cap_whose_default_is_the_settings_default() -> None:
    parameter = inspect.signature(install_error_handling).parameters["concurrency_cap"]

    assert parameter.default == Settings.model_fields["concurrency_cap"].default


def test_an_app_built_by_hand_with_a_given_cap_enforces_it() -> None:
    app = FastAPI()
    entered, release = threading.Event(), threading.Event()

    @app.get("/slow")
    def slow() -> dict:
        entered.set()
        assert release.wait(WAIT_SECONDS)
        return {}

    @app.get("/fast")
    def fast() -> dict:
        return {}

    install_error_handling(app, concurrency_cap=1)

    with (
        TestClient(app, raise_server_exceptions=False) as client,
        ThreadPoolExecutor(1) as executor,
    ):
        held = executor.submit(client.get, "/slow")
        try:
            assert entered.wait(WAIT_SECONDS)
            assert client.get("/fast").status_code == 503
        finally:
            release.set()
        assert held.result(timeout=WAIT_SECONDS).status_code == 200
