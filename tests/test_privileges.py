"""Exact privileges of the app role on every table, so a stray grant fails the build.

Each migration that creates a table must also add that table's expected privileges here.
The spec is docs/design.md (Tables) and CLAUDE.md: the app role owns nothing and has
explicit per-table grants; entries, transactions and accounts allow only SELECT and INSERT.
"""

import psycopg
import pytest

# table -> privileges the app role should hold, and nothing else. Add one entry per table.
EXPECTED_APP_PRIVILEGES: dict[str, set[str]] = {
    # Owned by the migration runner; the service has no business reading it.
    "schema_migrations": set(),
    # Insert and select only: currency and allow_negative are fixed for life.
    "accounts": {"SELECT", "INSERT"},
}

# Every non-owner entry in the table's ACL, including the PUBLIC pseudo-role.
TABLE_ACL = """
    SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type
    FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
    WHERE c.oid = %s::regclass AND a.grantee <> c.relowner
"""


def public_tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {r[0] for r in rows}


def test_every_table_declares_its_expected_privileges(owner_conn: psycopg.Connection) -> None:
    """A new table without an entry above would otherwise escape the privilege check."""
    assert public_tables(owner_conn) == set(EXPECTED_APP_PRIVILEGES)


@pytest.mark.parametrize("table", sorted(EXPECTED_APP_PRIVILEGES))
def test_app_role_has_exactly_the_expected_table_privileges(
    owner_conn: psycopg.Connection, table: str
) -> None:
    granted = set(owner_conn.execute(TABLE_ACL, (f"public.{table}",)).fetchall())
    expected = {("ledger_app", p) for p in EXPECTED_APP_PRIVILEGES[table]}
    assert granted == expected


@pytest.mark.parametrize("table", sorted(EXPECTED_APP_PRIVILEGES))
def test_no_column_level_grants(owner_conn: psycopg.Connection, table: str) -> None:
    """Column privileges are separate from table privileges and would slip past the ACL check."""
    rows = owner_conn.execute(
        "SELECT attname FROM pg_attribute"
        " WHERE attrelid = %s::regclass AND attacl IS NOT NULL AND NOT attisdropped",
        (f"public.{table}",),
    ).fetchall()
    assert rows == []


def test_no_sequence_grants(owner_conn: psycopg.Connection) -> None:
    """Identity columns are assigned by the database, so the app role needs no sequence rights."""
    rows = owner_conn.execute(
        "SELECT c.relname FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a"
        " WHERE c.relkind = 'S' AND c.relnamespace = 'public'::regnamespace"
        " AND a.grantee <> c.relowner"
    ).fetchall()
    assert rows == []
