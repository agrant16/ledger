"""Guard for tests/unit: a unit test never touches the database.

`uv run pytest tests/unit` is meant to work with no Postgres running. Nothing else stops a later
test from asking for a database fixture or connecting itself, and CI (which has a Postgres
service) would still pass, so the mistake would only show up on a machine with the database off.

- At collection, a unit test that depends on a database fixture stops the whole run with a usage
  error, so a violation fails CI.
- While a unit test runs, any attempt to open a psycopg connection raises.

The logic is in tests/db_guard.py; tests/unit/test_disallow_db.py tests both checks.
"""

from pathlib import Path

import pytest

from tests.db_guard import block_database_connections, find_database_users

UNIT_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # This hook receives every collected item, not only the ones under tests/unit.
    offenders = find_database_users(items, UNIT_DIR)
    if offenders:
        listing = "\n  ".join(offenders)
        raise pytest.UsageError(
            "Tests under tests/unit must not use the database. "
            f"Move them to tests/integration:\n  {listing}"
        )


@pytest.fixture(autouse=True)
def no_database_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    block_database_connections(monkeypatch)
