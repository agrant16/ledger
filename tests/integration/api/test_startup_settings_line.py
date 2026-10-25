"""The startup line that records the effective settings (docs/design.md, Request id and logging).

When the app starts it writes one INFO line with the pool size, the pool wait, the handler thread
limit, the concurrency cap and the readiness probe timeout, so the values behind a benchmark or
an incident can be read from the log. The line is a space-separated list of `name=value` pairs and
must stay extendable: milestone 4 adds the retry, delay and lock-timeout settings to it, so these
tests look up the pairs they need and ignore any others.

No correctness rule is involved. The database URLs hold passwords, so a test also checks that
nothing of them reaches the log.
"""

import logging
import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("migrated_database")

PAIR = re.compile(r"(\w+)=(\S+)")


def settings_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "concurrency_cap=" in r.getMessage()]


def started_settings(
    fresh_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture, **env: str
) -> logging.LogRecord:
    caplog.set_level(logging.INFO)
    with TestClient(fresh_app(**env)):
        pass
    (record,) = settings_records(caplog)
    return record


def test_startup_logs_one_info_line_with_the_effective_settings(
    fresh_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    record = started_settings(
        fresh_app,
        caplog,
        POOL_SIZE="3",
        POOL_TIMEOUT_SECONDS="1.5",
        HANDLER_THREAD_LIMIT="7",
        CONCURRENCY_CAP="5",
        HEALTH_TIMEOUT_SECONDS="3",
    )

    assert record.levelno == logging.INFO
    pairs = dict(PAIR.findall(record.getMessage()))
    assert pairs["pool_size"] == "3"
    assert pairs["pool_timeout_seconds"] == "1.5"
    assert pairs["handler_thread_limit"] == "7"
    assert pairs["concurrency_cap"] == "5"
    assert pairs["health_timeout_seconds"] == "3.0"


def test_the_line_shows_the_defaults_when_nothing_is_set(
    fresh_app: Callable[..., FastAPI],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in [
        "POOL_SIZE",
        "POOL_TIMEOUT_SECONDS",
        "HANDLER_THREAD_LIMIT",
        "CONCURRENCY_CAP",
        "HEALTH_TIMEOUT_SECONDS",
    ]:
        monkeypatch.delenv(name, raising=False)

    record = started_settings(fresh_app, caplog)

    pairs = dict(PAIR.findall(record.getMessage()))
    assert pairs["pool_size"] == "10"
    assert pairs["handler_thread_limit"] == "20"
    assert pairs["concurrency_cap"] == "40"
    assert "health_timeout_seconds" in pairs


def test_the_line_is_written_once_at_startup_and_not_per_request(
    fresh_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    with TestClient(fresh_app()) as client:
        assert len(settings_records(caplog)) == 1  # already there before the first request
        client.get("/health")
        client.get("/health")

    assert len(settings_records(caplog)) == 1


def test_the_line_does_not_leak_the_database_urls(
    fresh_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(fresh_app()):
        pass

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "postgresql://" not in text
    assert "dev-only" not in text
