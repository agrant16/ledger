"""check_database with a pool that fails on demand (ledger/infra/db.py).

The real-pool behavior is in tests/integration/db/test_check_database.py. Here a stand-in pool
shows what the function does with a failure it cannot cause for real: it asks for one connection,
runs one trivial statement, and lets any error through exactly as it was raised (no translation
and no swallowing), because /health reports a failure by failing.
"""

from contextlib import contextmanager

import psycopg
import pytest


class StatementRecorder:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.statements: list[str] = []

    def execute(self, query, *args, **kwargs):
        self.statements.append(str(query))
        if self.failure is not None:
            raise self.failure


class StandInPool:
    def __init__(self, connection: StatementRecorder, failure_on_checkout: Exception | None = None):
        self.connection_obj = connection
        self.failure_on_checkout = failure_on_checkout
        self.checkouts = 0

    @contextmanager
    def connection(self, *args, **kwargs):
        self.checkouts += 1
        if self.failure_on_checkout is not None:
            raise self.failure_on_checkout
        yield self.connection_obj


@pytest.fixture
def check_database():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.infra.db import check_database

    return check_database


def test_it_runs_one_statement_on_one_connection(check_database) -> None:
    recorder = StatementRecorder()
    pool = StandInPool(recorder)

    assert check_database(pool) is None

    assert pool.checkouts == 1
    assert recorder.statements == ["SELECT 1"]


@pytest.mark.parametrize(
    "failure",
    [
        psycopg.OperationalError("connection refused"),
        psycopg.errors.AdminShutdown("shutting down"),
        psycopg.errors.QueryCanceled("statement timeout"),
    ],
    ids=["unreachable", "shutdown", "timeout"],
)
def test_a_failing_statement_comes_out_as_it_was(check_database, failure: Exception) -> None:
    pool = StandInPool(StatementRecorder(failure))

    with pytest.raises(type(failure)) as caught:
        check_database(pool)

    assert caught.value is failure


def test_a_failure_to_get_a_connection_comes_out_as_it_was(check_database) -> None:
    failure = psycopg.OperationalError("no connection available")
    pool = StandInPool(StatementRecorder(), failure_on_checkout=failure)

    with pytest.raises(psycopg.OperationalError) as caught:
        check_database(pool)

    assert caught.value is failure
