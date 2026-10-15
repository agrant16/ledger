"""Resets the local development database and re-applies every migration (a manual-testing tool).

Run with: PYTHONPATH=src uv run python scripts/reset_db.py [--yes]

Drops every table and function in the public schema of the database named by DATABASE_OWNER_URL
(the local `ledger` database by default), then runs the migrations again from the first one. All
data is lost, identity counters start again at 1, and every grant to the app role is re-created
by the migrations. The database, its owner and the schema's access rules (docker/roles/roles.sql)
are left as they are.

It connects as the owner role, like the migration runner, and refuses any host that is not this
machine. The tests use their own database (`ledger_test`), which this script does not touch unless
DATABASE_OWNER_URL points at it.
"""

import argparse
import sys
from urllib.parse import urlsplit

import psycopg

from ledger.infra.config import load_settings
from ledger.migrate import run_migrations

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# quote_ident / %I quote the names, so a table or function with an unusual name is dropped safely.
SELECT_TABLES_SQL = """
SELECT quote_ident(tablename)
FROM pg_tables
WHERE schemaname = 'public';
"""

SELECT_FUNCTIONS_SQL = """
SELECT format('%I.%I(%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
FROM pg_proc AS p
INNER JOIN pg_namespace AS n ON p.pronamespace = n.oid
WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p');
"""


def check_local(owner_url: str) -> str:
    """Return the database name, or exit if the URL does not point at this machine."""
    parts = urlsplit(owner_url)
    if parts.hostname not in LOCAL_HOSTS:
        sys.exit(f"Refusing to reset a database on {parts.hostname!r}: only local databases.")
    return parts.path.lstrip("/")


def drop_everything(owner_url: str) -> None:
    """Drop all tables (CASCADE, which also removes their identity sequences and triggers), then
    all functions, in one transaction, so a failure leaves the database as it was."""
    with psycopg.connect(owner_url) as conn:
        tables = [row[0] for row in conn.execute(SELECT_TABLES_SQL)]
        functions = [row[0] for row in conn.execute(SELECT_FUNCTIONS_SQL)]
        for table in tables:
            conn.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
        for function in functions:
            conn.execute(f"DROP ROUTINE IF EXISTS {function} CASCADE")
        print(f"dropped {len(tables)} table(s) and {len(functions)} function(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    settings = load_settings()
    database = check_local(settings.owner_url)

    if not args.yes:
        answer = input(
            f"This deletes ALL data in database {database!r} and re-runs the migrations."
            " Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1

    drop_everything(settings.owner_url)
    for name in run_migrations(settings.owner_url, settings.migrations_dir):
        print(f"applied {name}")
    print(f"database {database!r} reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
