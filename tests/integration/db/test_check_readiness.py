"""check_readiness (ledger/infra/readiness.py; docs/design.md, API: /health; ADR 0004).

    check_readiness(app_url: str, timeout_seconds: float) -> ReadinessResult

Contract these tests expect:

- It opens its own connection with `psycopg.connect` (not the request pool), so a busy pool can
  never make it fail, and closes it before returning.
- It runs its checks in this order and stops at the first failure: `SELECT 1` ("database"),
  `SHOW transaction_read_only`, which must be `off` ("writable"), then a `SELECT 1 FROM <table>
  LIMIT 1` and a `has_table_privilege` check for each of accounts, transactions, entries and
  balances ("tables"). It never writes, because a write would use up identity ids.
- It returns a `ReadinessResult` with `failed_check` (None, or one of those three names), `cause`
  (the psycopg error behind a failure, else None) and `ready` (true when nothing failed).
- A `psycopg.Error` at any stage is a not-ready result and is never raised. Anything else is a bug
  and propagates.
- `timeout_seconds` bounds the connect and each statement. libpq never waits less than 2 seconds
  for a connection (a connect_timeout of 1 is read as 2), so the tests use 2 seconds.

Failure paths that need an error a real database cannot be made to give are in
tests/unit/test_check_readiness_errors.py.

No correctness rule is involved: this is the readiness probe (issue #155).
"""

import socket
import time
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.db_helpers import with_database

TIMEOUT = 2.0
# How much longer than the timeout a failed check may take (process and network overhead).
MARGIN = 1.5

# The grants the service needs, table -> privileges (the same as the app role's grants in
# tests/integration/db/test_privileges.py).
NEEDED_GRANTS = {
    "accounts": ("SELECT", "INSERT"),
    "transactions": ("SELECT", "INSERT"),
    "entries": ("SELECT", "INSERT"),
    "balances": ("SELECT", "INSERT", "UPDATE"),
}
GRANT_CASES = [(table, grant) for table, grants in NEEDED_GRANTS.items() for grant in grants]
TABLES_WITH_IDS = ("accounts", "transactions", "entries")


@pytest.fixture
def readiness():
    """The module under test. Imported lazily so these tests fail one by one while the module does
    not exist yet."""
    from ledger.infra import readiness as module  # noqa: PLC0415

    return module


def with_host_and_port(url: str, port: int) -> str:
    """The URL pointing at 127.0.0.1 on this port, with the credentials and database unchanged."""
    parts = urlsplit(url)
    userinfo = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{port}"))


@pytest.fixture
def closed_port_url(settings) -> str:
    """A URL for a port nothing listens on: the connection is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return with_host_and_port(settings.app_url, port)


@pytest.fixture
def black_hole_url(settings) -> Iterator[str]:
    """A URL for a socket that completes the TCP handshake and never says anything, like a database
    that is hung: a connect waits until its timeout."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield with_host_and_port(settings.app_url, server.getsockname()[1])


@pytest.fixture
def read_only_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every connection `psycopg.connect` opens from now on is read-only, as on a standby after a
    failover: the server itself reports `transaction_read_only = on`.

    The option is added to each connection instead of being set on the database or the role, so
    nothing is stored in the database and a run that is killed during the test leaves nothing
    behind (a database-level setting would make every later connection read-only, the owner's
    too).
    """
    real_connect = psycopg.connect

    def connect_read_only(*args, **kwargs):
        options = f"{kwargs.get('options', '')} -c default_transaction_read_only=on".strip()
        return real_connect(*args, **{**kwargs, "options": options})

    monkeypatch.setattr(psycopg, "connect", connect_read_only)


@pytest.fixture
def revoke(owner_conn: psycopg.Connection) -> Iterator[Callable[[str, str], None]]:
    """Revoke one privilege from the app role as the owner. Every revoked privilege is granted
    again at teardown, so a failing test cannot leave the database without it."""
    revoked: list[tuple[str, str]] = []

    def take_away(table: str, privilege: str) -> None:
        owner_conn.execute(f"REVOKE {privilege} ON {table} FROM ledger_app")
        revoked.append((table, privilege))

    try:
        yield take_away
    finally:
        for table, privilege in revoked:
            owner_conn.execute(f"GRANT {privilege} ON {table} TO ledger_app")


@pytest.fixture
def balances_locked(settings) -> Iterator[None]:
    """Another session holds an exclusive lock on balances, so a read of it waits."""
    with psycopg.connect(settings.owner_url) as holder:
        holder.execute("LOCK TABLE balances IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            holder.rollback()


def test_a_healthy_database_is_ready(readiness, settings) -> None:
    result = readiness.check_readiness(settings.app_url, TIMEOUT)

    assert result.ready is True
    assert result.failed_check is None
    assert result.cause is None


def test_a_probe_writes_nothing_so_it_burns_no_ids(
    readiness, settings, owner_conn: psycopg.Connection
) -> None:
    """A write probe, even one that is rolled back, would use up identity values. Every identity
    sequence is exactly where it was after many probes."""

    def sequences() -> list[tuple]:
        return [
            owner_conn.execute(f"SELECT last_value, is_called FROM {table}_id_seq").fetchone()
            for table in TABLES_WITH_IDS
        ]

    before = sequences()
    for _ in range(20):
        assert readiness.check_readiness(settings.app_url, TIMEOUT).ready

    assert sequences() == before


def test_every_probe_closes_its_connection(
    readiness, settings, owner_conn: psycopg.Connection
) -> None:
    """The app role has no more sessions after 20 probes than before, so none is leaked."""
    role = conninfo_to_dict(settings.app_url)["user"]

    def sessions() -> int:
        row = owner_conn.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE usename = %s AND datname = current_database()",
            (role,),
        ).fetchone()
        return row[0]

    before = sessions()
    for _ in range(20):
        readiness.check_readiness(settings.app_url, TIMEOUT)

    deadline = time.monotonic() + 3  # a closed connection's backend takes a moment to exit
    while sessions() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sessions() <= before  # not "==": a session left by an earlier test may exit meanwhile


def test_an_unreachable_database_is_not_ready_and_the_error_is_kept(
    readiness, closed_port_url: str
) -> None:
    started = time.monotonic()
    result = readiness.check_readiness(closed_port_url, TIMEOUT)

    assert result.ready is False
    assert result.failed_check == "database"
    assert isinstance(result.cause, psycopg.OperationalError)
    assert time.monotonic() - started < TIMEOUT + MARGIN


def test_a_database_that_never_answers_is_not_ready_within_the_timeout(
    readiness, black_hole_url: str
) -> None:
    """The bound that has to stay below a probe's timeoutSeconds: a hung database must not hold the
    probe for longer than the timeout."""
    started = time.monotonic()
    result = readiness.check_readiness(black_hole_url, TIMEOUT)
    elapsed = time.monotonic() - started

    assert result.ready is False
    assert result.failed_check == "database"
    assert isinstance(result.cause, psycopg.OperationalError)
    assert elapsed < TIMEOUT + MARGIN


@pytest.mark.usefixtures("read_only_connections")
def test_a_read_only_database_is_not_ready(readiness, settings) -> None:
    """The service cannot post a transfer to a database that refuses writes, as after a failover to
    a standby, even though `SELECT 1` works."""
    result = readiness.check_readiness(settings.app_url, TIMEOUT)

    assert result.ready is False
    assert result.failed_check == "writable"


def test_a_database_without_the_ledger_tables_is_not_ready(readiness, settings) -> None:
    """The `postgres` database answers and is writable, but has none of the ledger's tables."""
    url = with_database(settings.app_url, "postgres")

    result = readiness.check_readiness(url, TIMEOUT)

    assert result.ready is False
    assert result.failed_check == "tables"
    assert isinstance(result.cause, psycopg.errors.UndefinedTable)


@pytest.mark.parametrize("table, privilege", GRANT_CASES)
def test_a_missing_grant_is_not_ready(
    readiness, settings, revoke: Callable[[str, str], None], table: str, privilege: str
) -> None:
    assert readiness.check_readiness(
        settings.app_url, TIMEOUT
    ).ready  # the control: it starts ready
    revoke(table, privilege)

    result = readiness.check_readiness(settings.app_url, TIMEOUT)

    assert result.ready is False
    assert result.failed_check == "tables"


@pytest.mark.usefixtures("balances_locked")
def test_a_read_that_waits_on_a_lock_is_cut_off_by_the_statement_timeout(
    readiness, settings
) -> None:
    started = time.monotonic()
    result = readiness.check_readiness(settings.app_url, TIMEOUT)
    elapsed = time.monotonic() - started

    assert result.ready is False
    assert result.failed_check == "tables"
    assert isinstance(result.cause, psycopg.errors.QueryCanceled)
    assert elapsed < TIMEOUT + MARGIN


def test_an_error_that_is_not_a_database_error_propagates(
    readiness, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in the check is not hidden as "not ready": the route turns it into a logged 500."""

    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(psycopg, "connect", broken)

    with pytest.raises(RuntimeError, match="a bug"):
        readiness.check_readiness(settings.app_url, TIMEOUT)
