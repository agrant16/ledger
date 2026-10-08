"""Shared fixtures. Tests run against a real Postgres (`docker compose up -d db` locally).

Two roles, as in production:
- owner_conn: migrations, resetting and seeding data. Never used by code under test.
- app_pool / app_conn: the role the service connects as. Everything under test uses these.
"""

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.config import Settings, load_settings
from ledger.migrate import run_migrations

# Bookkeeping table owned by the migration runner; resetting must not drop its rows.
PRESERVED_TABLES = ("schema_migrations",)


def reset_database(conn: psycopg.Connection) -> None:
    """Empty every application table and restart identity counters.

    Call this from inside a Hypothesis test body: function-scoped fixtures run once per
    test function, not once per generated example.
    """
    rows = conn.execute(
        "SELECT quote_ident(tablename) FROM pg_tables"
        " WHERE schemaname = 'public' AND tablename <> ALL(%s)",
        (list(PRESERVED_TABLES),),
    ).fetchall()
    if rows:
        tables = ", ".join(r[0] for r in rows)
        conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session", autouse=True)
def migrated_database(settings: Settings) -> None:
    run_migrations(settings.owner_url, settings.migrations_dir)


@pytest.fixture
def owner_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.owner_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean_db(owner_conn: psycopg.Connection) -> None:
    """Start the test with all application tables empty."""
    reset_database(owner_conn)


@pytest.fixture(scope="session")
def app_pool(settings: Settings) -> Iterator[ConnectionPool]:
    with ConnectionPool(settings.app_url, min_size=1, max_size=10, open=True) as pool:
        yield pool


@pytest.fixture
def app_conn(app_pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    with app_pool.connection() as conn:
        yield conn
