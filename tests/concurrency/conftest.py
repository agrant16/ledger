"""Fixtures for the concurrency tests: a fixed pool of 50 app-role connections and the run's seed.

The session-wide `app_pool` in tests/conftest.py stays small. This pool is module-scoped, so the
50 connections are opened once per test module that uses it and closed when the module ends.
"""

import os
from collections.abc import Iterator
from types import ModuleType

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.infra.config import Settings


@pytest.fixture(scope="session")
def harness() -> ModuleType:
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from tests.concurrency import harness as module  # noqa: PLC0415

    return module


def _server_connection_limits(settings: Settings) -> tuple[int, int]:
    """The server's `max_connections` and `superuser_reserved_connections`."""
    with psycopg.connect(settings.owner_url, autocommit=True) as conn:
        values = []
        for setting in ("max_connections", "superuser_reserved_connections"):
            row = conn.execute(f"SHOW {setting}").fetchone()
            assert row is not None
            values.append(int(row[0]))
    return values[0], values[1]


@pytest.fixture(scope="module")
def concurrency_pool(
    settings: Settings, migrated_database: None, harness: ModuleType
) -> Iterator[ConnectionPool]:
    """A fixed pool of `CONCURRENCY_THREADS` app-role connections, open and waited for.

    Fails with a clear message when the server cannot hold the pool next to the other pools of a
    test run, and does not raise `max_connections` itself.
    """
    max_connections, reserved = _server_connection_limits(settings)
    harness.check_connection_budget(max_connections, reserved)
    size = harness.CONCURRENCY_THREADS
    with ConnectionPool(
        settings.app_url,
        min_size=size,
        max_size=size,
        timeout=harness.POOL_TIMEOUT_SECONDS,
        open=True,
    ) as pool:
        pool.wait(timeout=harness.POOL_TIMEOUT_SECONDS)
        yield pool


@pytest.fixture
def seed(harness: ModuleType, record_property) -> int:
    """The seed for this test's random data: `CONCURRENCY_SEED` if set, else a fresh one.

    Printed and recorded, so a failure shows it and the run can be repeated with the same data.
    Thread scheduling is not reproducible from a seed.
    """
    value = harness.choose_seed(os.environ)
    record_property("seed", value)
    print(f"{harness.SEED_VARIABLE}={value}")
    return value
