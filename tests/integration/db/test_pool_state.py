"""pool_state (ledger/infra/readiness.py; docs/design.md, API: /health).

    pool_state(pool) -> "ok" | "saturated"

Reports whether the request pool has a free connection, for the `pool` field of the `/health`
response. It reads the pool's own counters and never takes a connection, so asking can neither
wait nor fail. The service's pool has a fixed size, so "saturated" means every connection is in
use. A saturated pool is information, not a failure: the concurrency cap and `Retry-After` handle
it request by request.

No correctness rule is involved: this is the readiness probe (issue #155).
"""

import time
from collections.abc import Iterator

import pytest
from psycopg_pool import ConnectionPool

SIZE = 2


@pytest.fixture
def readiness():
    """The module under test. Imported lazily so these tests fail one by one while the module does
    not exist yet."""
    from ledger.infra import readiness as module  # noqa: PLC0415

    return module


@pytest.fixture
def small_pool(settings) -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.app_url, min_size=SIZE, max_size=SIZE, timeout=0.5, open=True
    ) as pool:
        pool.wait()
        yield pool


def test_an_idle_pool_is_ok(readiness, small_pool: ConnectionPool) -> None:
    assert readiness.pool_state(small_pool) == "ok"


def test_a_pool_with_one_connection_free_is_ok(readiness, small_pool: ConnectionPool) -> None:
    with small_pool.connection():
        assert readiness.pool_state(small_pool) == "ok"


def test_a_pool_with_every_connection_in_use_is_saturated_and_answers_at_once(
    readiness, small_pool: ConnectionPool
) -> None:
    with small_pool.connection(), small_pool.connection():
        started = time.monotonic()
        state = readiness.pool_state(small_pool)
        elapsed = time.monotonic() - started

    assert state == "saturated"
    assert elapsed < 0.25, "it must not wait for a connection (the pool wait is 0.5 seconds)"
    assert readiness.pool_state(small_pool) == "ok"  # the control: free again
