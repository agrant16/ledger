"""Helpers shared by the tests that use the database."""

import psycopg

# Bookkeeping table owned by the migration runner; resetting must not drop its rows.
PRESERVED_TABLES = ("schema_migrations",)

# psycopg waits 130 seconds for a connection when none is set, so a stopped database would make
# the first test that needs it wait over two minutes before failing.
CONNECT_TIMEOUT_SECONDS = 5


def with_connect_timeout(url: str, seconds: int = CONNECT_TIMEOUT_SECONDS) -> str:
    """The URL with a connect_timeout in seconds, unless it already sets one."""
    if "connect_timeout=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}connect_timeout={seconds}"


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
