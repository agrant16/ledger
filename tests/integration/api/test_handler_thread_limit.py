"""The handler thread limit (docs/design.md, Stack decisions; ADR 0004).

Synchronous handlers run in anyio's worker threads, whose default limit is 40. The service sets it
to 20 at startup (in the lifespan), from the HANDLER_THREAD_LIMIT environment variable, so the 10
pooled connections are not outnumbered by many more waiting threads. The limiter belongs to the
event loop, so each started app gets a fresh one and these tests read it inside the running app.
"""

import pytest
from anyio import to_thread
from fastapi.testclient import TestClient

from ledger.main import app

pytestmark = pytest.mark.usefixtures("migrated_database")


async def current_thread_limit() -> float:
    return to_thread.current_default_thread_limiter().total_tokens


def thread_limit_after_startup() -> float:
    with TestClient(app) as client:
        return client.portal.call(current_thread_limit)


def test_the_thread_limit_is_20_after_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HANDLER_THREAD_LIMIT", raising=False)

    assert thread_limit_after_startup() == 20


def test_the_thread_limit_follows_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HANDLER_THREAD_LIMIT", "7")

    assert thread_limit_after_startup() == 7
