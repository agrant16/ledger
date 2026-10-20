"""Fixtures shared by the API integration tests that need the real app with chosen settings."""

import importlib
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI

import ledger.main


@pytest.fixture
def fresh_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., FastAPI]]:
    """Build the real app (`ledger.main.app`) with environment variables set first.

    `ledger.main` is imported once per test session, and Starlette builds an app's middleware stack
    on its first request and keeps it. Reloading the module gives each test its own app, so a
    setting that is read when the module is imported (a middleware argument) and one that is read
    at startup (the lifespan) both take the values the test set. The module is reloaded again on
    teardown, with the environment restored, so no later test sees this test's settings.
    """

    def build(**env: str) -> FastAPI:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        importlib.reload(ledger.main)
        return ledger.main.app

    yield build

    monkeypatch.undo()
    importlib.reload(ledger.main)
