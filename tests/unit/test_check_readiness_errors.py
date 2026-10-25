"""check_readiness with a connection that fails on demand (ledger/infra/readiness.py).

The behavior against a real database is in tests/integration/db/test_check_readiness.py. Here a
stand-in connection shows what the function does with errors a real database cannot be made to
give on request: every `psycopg.Error` becomes a not-ready result that keeps the error as its
cause, the failed check is named by the stage where the error happened, the connection is closed
even then, and an error that is not a database error is a bug and propagates.

Contract these tests expect, in addition to the one in the integration file: the function opens
the connection with `psycopg.connect` and runs its statements with `conn.execute`, in the order
`SELECT 1`, `SHOW transaction_read_only`, then the table checks.
"""

import psycopg
import pytest

TIMEOUT = 2.0


class Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class StandInConnection:
    """Answers every statement as a healthy database would, except that the statement with the
    number `fail_at` (counting from 1) raises `error`."""

    def __init__(self, fail_at: int | None, error: Exception | None) -> None:
        self.fail_at = fail_at
        self.error = error
        self.statements: list[str] = []
        self.closed = False

    def execute(self, query, params=None, **kwargs) -> Rows:
        self.statements.append(str(query))
        if len(self.statements) == self.fail_at:
            raise self.error
        if "transaction_read_only" in str(query):
            return Rows([("off",)])
        return Rows([(True,)])

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


@pytest.fixture
def readiness():
    """The module under test. Imported lazily so these tests fail one by one while the module does
    not exist yet."""
    from ledger.infra import readiness as module  # noqa: PLC0415

    return module


ERRORS = [
    psycopg.OperationalError("connection refused"),
    psycopg.errors.AdminShutdown("shutting down"),
    psycopg.errors.QueryCanceled("statement timeout"),
    psycopg.errors.InsufficientPrivilege("permission denied for table balances"),
    psycopg.errors.UndefinedTable('relation "balances" does not exist'),
]


@pytest.mark.parametrize("error", ERRORS, ids=lambda error: type(error).__name__)
def test_an_error_while_connecting_is_a_not_ready_result_with_the_error_as_its_cause(
    readiness, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(psycopg, "connect", refuse)

    result = readiness.check_readiness("postgresql://unused", TIMEOUT)

    assert result.ready is False
    assert result.failed_check == "database"
    assert result.cause is error


@pytest.mark.parametrize(
    "fail_at, failed_check",
    [(1, "database"), (2, "writable"), (3, "tables"), (4, "tables")],
    ids=["select-1", "read-only-check", "first-table-check", "later-table-check"],
)
def test_the_failed_check_is_named_by_the_stage_where_the_error_happened(
    readiness, monkeypatch: pytest.MonkeyPatch, fail_at: int, failed_check: str
) -> None:
    error = psycopg.errors.QueryCanceled("statement timeout")
    connection = StandInConnection(fail_at, error)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)

    result = readiness.check_readiness("postgresql://unused", TIMEOUT)

    assert result.ready is False
    assert result.failed_check == failed_check
    assert result.cause is error


def test_the_connection_is_closed_even_when_a_check_fails(
    readiness, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = StandInConnection(2, psycopg.OperationalError("server closed the connection"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)

    readiness.check_readiness("postgresql://unused", TIMEOUT)

    assert connection.closed


def test_an_error_that_is_not_a_database_error_in_a_statement_propagates(
    readiness, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = StandInConnection(2, RuntimeError("a bug"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(RuntimeError, match="a bug"):
        readiness.check_readiness("postgresql://unused", TIMEOUT)

    assert connection.closed
