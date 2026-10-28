"""The isolation level of a request's transaction (docs/design.md, Milestone 4; ADR 0006; rule 4).

The locking in ADR 0006 is only correct at READ COMMITTED: each `SELECT ... FOR UPDATE` must see
the committed balance once it holds the lock, and the overdraft check reads the balance that same
statement returns. Under a stricter level the same code would meet serialization failures (40001)
that nothing retries, and under a weaker one it would not be the design the ADR describes. The
service never sets a level, so it relies on the server default; these tests pin the level a request
really gets, so a changed default fails here and not as a race in production.

- A write transaction run through `run_in_transaction` on the production pool (`create_pool`)
  reports `read committed`.
- So does a `read_only=True` transaction.
- Control: the same check against a pool whose connections default to SERIALIZABLE reports
  `serializable`, so the assertions above are shown able to fail.
"""

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.infra.config import Settings
from ledger.infra.db import Pool, create_pool
from ledger.infra.db import run_in_transaction as run

READ_COMMITTED = "read committed"


@pytest.fixture
def production_pool(settings: Settings, migrated_database: None) -> Iterator[Pool]:
    """The pool the service builds at startup, opened and waited for."""
    pool = create_pool(settings)
    pool.open(wait=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def serializable_pool(settings: Settings, migrated_database: None) -> Iterator[ConnectionPool]:
    """A pool whose connections default to SERIALIZABLE, for the control."""
    with ConnectionPool(
        settings.app_url,
        min_size=1,
        max_size=1,
        kwargs={"options": "-c default_transaction_isolation=serializable"},
        open=True,
    ) as pool:
        yield pool


def effective_level(conn: psycopg.Connection) -> str:
    row = conn.execute("SHOW transaction_isolation").fetchone()
    assert row is not None
    return row[0]


def test_a_write_transaction_runs_at_read_committed(production_pool: Pool) -> None:
    assert run(production_pool, effective_level) == READ_COMMITTED


def test_a_read_only_transaction_runs_at_read_committed(production_pool: Pool) -> None:
    assert run(production_pool, effective_level, read_only=True) == READ_COMMITTED


def test_control_a_serializable_default_is_seen_by_the_same_check(
    serializable_pool: ConnectionPool,
) -> None:
    assert run(serializable_pool, effective_level) == "serializable"
    assert run(serializable_pool, effective_level, read_only=True) == "serializable"
