"""The role setup in docker/roles/roles.sql: the app role must be unable to change schema."""

import psycopg
import pytest


def test_app_role_is_not_superuser_and_owns_nothing(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection
) -> None:
    row = app_conn.execute(
        "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert row == (False, False, False)

    owned = owner_conn.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner"
        " WHERE r.rolname = 'ledger_app'"
    ).fetchone()
    assert owned == (0,)


def test_app_role_cannot_create_tables(app_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("CREATE TABLE should_not_exist (x int)")


def test_connections_use_the_expected_roles(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection
) -> None:
    assert app_conn.execute("SELECT current_user").fetchone() == ("ledger_app",)
    assert owner_conn.execute("SELECT current_user").fetchone() == ("ledger_owner",)
