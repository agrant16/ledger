"""Fixtures shared by the API integration tests that need the real app with chosen settings."""

from collections.abc import Callable

import pytest
from fastapi import FastAPI

import ledger.main


@pytest.fixture
def fresh_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    """Build the real app with environment variables set first.

    The factory (`ledger.main.create_app`) loads the settings once, when it builds the app, so
    setting the variables and then building gives the test an app that uses them. Each call builds
    a new app, so no module reload is needed and the environment is restored by `monkeypatch` at
    teardown.
    """

    def build(**env: str) -> FastAPI:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return ledger.main.create_app()

    return build
