"""Keeping the database away from the unit tests (the guard itself is tests/unit/conftest.py).

Two checks, because a test can reach the database in two ways: by asking for a database fixture,
which can be seen when the tests are collected, and by connecting itself, which can only be stopped
while the test runs.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg
import pytest

# The base fixtures every other database fixture (app_conn, clean_db, commit_setup, ...) is built
# on. A new fixture that opens its own connection must depend on one of these, or be added here.
DATABASE_FIXTURES = {"migrated_database", "owner_conn", "app_pool"}

REFUSAL = "Unit tests must not connect to the database. Move this test to tests/integration."


def find_database_users(items: Iterable[Any], directory: Path) -> list[str]:
    """Describe each collected test under `directory` that depends on a database fixture.

    `item.fixturenames` is the whole closure of fixtures a test depends on, so a fixture built on
    one of the base ones is caught too.
    """
    directory = directory.resolve()
    offenders = []
    for item in items:
        if not item.path.resolve().is_relative_to(directory):
            continue
        used = DATABASE_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if used:
            offenders.append(f"{item.nodeid} (uses {', '.join(sorted(used))})")
    return offenders


def refuse_connection(*args: Any, **kwargs: Any) -> None:
    raise AssertionError(REFUSAL)


def block_database_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every way psycopg opens a connection raise. psycopg.connect is a separate reference to
    Connection.connect, so both are patched, and the pool opens connections through the class."""
    monkeypatch.setattr(psycopg, "connect", refuse_connection)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refuse_connection))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(refuse_connection))
