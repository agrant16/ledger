"""Guard for tests/unit: a unit test never touches the database.

`uv run pytest tests/unit` is meant to work with no Postgres running. Nothing stops a later test
from asking for a database fixture, and CI (which has a Postgres service) would still pass, so
the mistake would only show up on a machine with the database off. This fails collection instead.

`item.fixturenames` is the full closure of fixtures a test depends on, so a fixture built on one
of these (app_conn, clean_db, commit_setup, ...) is caught too. New database fixtures will
depend on at least one of the base three below.
"""

from pathlib import Path

import pytest

DATABASE_FIXTURES = {"migrated_database", "owner_conn", "app_pool"}
UNIT_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    offenders = []
    for item in items:
        # This hook receives every collected item, not only the ones under tests/unit.
        if not item.path.is_relative_to(UNIT_DIR):
            continue
        used = DATABASE_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if used:
            offenders.append(f"{item.nodeid} (uses {', '.join(sorted(used))})")
    if offenders:
        listing = "\n  ".join(offenders)
        raise pytest.UsageError(
            "Tests under tests/unit must not use the database. Move them to "
            f"tests/integration:\n  {listing}"
        )
