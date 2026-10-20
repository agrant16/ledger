"""Shared fixtures. Tests run against a real Postgres (`docker compose up -d db` locally).

Nothing here runs unless a test asks for it, so the tests under tests/unit need no database. The
database fixtures (owner_conn, app_pool and the ones built on them) first apply the migrations.

Two roles, as in production:
- owner_conn: migrations, resetting and seeding data. Never used by code under test.
- app_pool / app_conn: the role the service connects as. Everything under test uses these.
"""

import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.infra.config import Settings, load_settings
from ledger.migrate import run_migrations
from ledger.service.account_service import create_account
from tests.db_helpers import (
    TEST_DATABASE_NAME,
    reset_database,
    with_connect_timeout,
    with_database,
)

# pytest's own plugin for testing pytest behavior, used by tests/unit/test_disallow_db.py. It has to
# be declared in the top-level conftest.
pytest_plugins = ["pytester"]


@pytest.fixture(scope="session", autouse=True)
def use_the_test_database() -> Iterator[None]:
    """Point every `load_settings()` call in the test run at the test database.

    Tests empty the tables they use, so they must never run against the database the service is
    using. The same server, roles and passwords are used; only the database name changes. Setting
    the environment, instead of building settings only in the `settings` fixture, also covers code
    that reads its own settings: the real app's startup and the property test.
    """
    configured = load_settings()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_OWNER_URL", with_database(configured.owner_url, TEST_DATABASE_NAME))
        patch.setenv("DATABASE_APP_URL", with_database(configured.app_url, TEST_DATABASE_NAME))
        yield


@pytest.fixture(scope="session")
def settings(use_the_test_database: None) -> Settings:
    """The settings with a short connect timeout, so a stopped database fails in seconds."""
    configured = load_settings()
    return configured.model_copy(
        update={
            "owner_url": with_connect_timeout(configured.owner_url),
            "app_url": with_connect_timeout(configured.app_url),
        }
    )


@pytest.fixture(scope="session")
def migrated_database(settings: Settings) -> None:
    try:
        run_migrations(settings.owner_url, settings.migrations_dir)
    except psycopg.OperationalError as failure:
        if f'database "{TEST_DATABASE_NAME}" does not exist' not in str(failure):
            raise
        raise RuntimeError(
            f"The test database {TEST_DATABASE_NAME!r} does not exist. A fresh Compose volume "
            "creates it; an older one needs it added once (see 'Running tests' in the README)."
        ) from failure


@pytest.fixture
def owner_conn(settings: Settings, migrated_database: None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.owner_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def scratch_url(settings: Settings, owner_conn: psycopg.Connection) -> Iterator[str]:
    """Owner-role URL whose search_path is a throwaway schema, so the runner's bookkeeping
    and the test migrations never touch the real tables."""
    schema = f"mig_test_{uuid.uuid4().hex[:8]}"
    owner_conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in settings.owner_url else "?"
    yield f"{settings.owner_url}{sep}options=-csearch_path%3D{schema}"
    owner_conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def clean_db(owner_conn: psycopg.Connection) -> None:
    """Start the test with all application tables empty."""
    reset_database(owner_conn)


@pytest.fixture(scope="session")
def app_pool(settings: Settings, migrated_database: None) -> Iterator[ConnectionPool]:
    with ConnectionPool(settings.app_url, min_size=1, max_size=10, open=True) as pool:
        yield pool


@pytest.fixture
def app_conn(app_pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """An app-role connection whose work is rolled back when the test ends.

    The pool would otherwise commit on a clean exit. Rolling back leaves no data behind, and keeps
    tests clear of the commit-time triggers that reject a transaction with no entries or with
    entries that do not sum to zero. A test that really needs committed data must call
    conn.commit() itself, and commit only balanced postings.
    """
    with app_pool.connection() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def make_funding_account(app_conn: psycopg.Connection):
    """Create the allow_negative account that issues money in a currency, through create_account.

    There is no deposit endpoint: money enters through transfers out of one funding account per
    currency, which goes negative by exactly what it has issued.
    """

    def make(currency: str = "USD"):
        return create_account(app_conn, f"funding:{currency}", currency, True)

    return make


@pytest.fixture
def commit_setup(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection
) -> Iterator[Callable[[], None]]:
    """Return a function that commits what the test has written so far, so another connection can
    see it (a second connection cannot see uncommitted accounts).

    Use it only for accounts and balances rows. Transactions and entries are checked at commit by
    the deferred constraint triggers, so a test must not commit those unless the commit is the
    thing under test (integration/db/test_constraint_triggers.py). Everything is truncated again
    afterwards, after releasing whatever locks the test still holds, or the truncate would wait for
    them.
    """
    yield app_conn.commit
    app_conn.rollback()
    reset_database(owner_conn)
