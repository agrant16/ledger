"""Properties of the accounts table (docs/design.md, Tables) checked through the app role."""

from pathlib import Path

import psycopg
import pytest

from ledger.config import Settings
from ledger.migrate import run_migrations

INSERT = (
    "INSERT INTO accounts (account_name, currency, allow_negative) VALUES (%s, %s, %s) RETURNING id"
)


def unique_column_sets(conn: psycopg.Connection) -> set[tuple[str, ...]]:
    """Column sets of every unique index on accounts, primary key included."""
    rows = conn.execute(
        "SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_index i"
        " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
        " WHERE i.indrelid = 'public.accounts'::regclass AND i.indisunique"
        " GROUP BY i.indexrelid"
    ).fetchall()
    return {tuple(r[0]) for r in rows}


def test_ids_are_database_assigned_bigint_identity(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type, is_identity, identity_generation FROM information_schema.columns"
        " WHERE table_name = 'accounts' AND column_name = 'id'"
    ).fetchone()
    assert row == ("bigint", "YES", "ALWAYS")


def test_account_can_be_created_and_gets_an_id(clean_db: None, app_conn: psycopg.Connection):
    first = app_conn.execute(INSERT, ("customer:alice", "USD", False)).fetchone()
    second = app_conn.execute(INSERT, ("customer:bob", "USD", False)).fetchone()
    assert first is not None and second is not None
    assert first[0] >= 1
    assert second[0] > first[0]


def test_caller_cannot_choose_the_id(clean_db: None, app_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.GeneratedAlways):
        app_conn.execute(
            "INSERT INTO accounts (id, account_name, currency, allow_negative)"
            " VALUES (7, 'x', 'USD', false)"
        )


def test_duplicate_name_is_rejected(clean_db: None, app_conn: psycopg.Connection) -> None:
    app_conn.execute(INSERT, ("customer:alice", "USD", False))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(INSERT, ("customer:alice", "EUR", False))


def test_names_are_case_sensitive(clean_db: None, app_conn: psycopg.Connection) -> None:
    app_conn.execute(INSERT, ("customer:alice", "USD", False))
    app_conn.execute(INSERT, ("Customer:Alice", "USD", False))


def test_unique_keys_back_the_composite_foreign_keys(owner_conn: psycopg.Connection) -> None:
    """(id, currency) backs entries' foreign key; (id, allow_negative) backs balances'."""
    sets = unique_column_sets(owner_conn)
    assert ("id",) in sets
    assert ("account_name",) in sets
    assert ("currency", "id") in sets
    assert ("allow_negative", "id") in sets


@pytest.mark.parametrize(
    "values",
    [
        (None, "USD", False),
        ("customer:alice", None, False),
        # A NULL allow_negative would slip past CHECK (allow_negative OR balance_minor >= 0).
        ("customer:alice", "USD", None),
    ],
    ids=["account_name", "currency", "allow_negative"],
)
def test_columns_are_not_null(
    clean_db: None, app_conn: psycopg.Connection, values: tuple[str | None, str | None, bool | None]
) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        app_conn.execute(INSERT, values)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE accounts SET currency = 'EUR'",
        "UPDATE accounts SET allow_negative = true",
        "DELETE FROM accounts",
        "TRUNCATE accounts",
    ],
)
def test_accounts_are_immutable_to_the_app_role(
    clean_db: None, app_conn: psycopg.Connection, statement: str
) -> None:
    """Currency and allow_negative are fixed for life because the app role cannot change rows."""
    app_conn.execute(INSERT, ("customer:alice", "USD", False))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)


# The API validates these too (docs/design.md, Account request); the database repeats the rules
# so no writer can bypass them.
@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY"])
def test_valid_currency_is_accepted(
    clean_db: None, app_conn: psycopg.Connection, currency: str
) -> None:
    app_conn.execute(INSERT, ("customer:alice", currency, False))


@pytest.mark.parametrize(
    "currency",
    ["usd", "Usd", "US", "USDX", "U5D", "US$", "U D", "", " USD", "USD\n"],
    ids=repr,
)
def test_currency_must_be_three_uppercase_letters(
    clean_db: None, app_conn: psycopg.Connection, currency: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(INSERT, ("customer:alice", currency, False))


@pytest.mark.parametrize("name", ["a", "customer:alice", "x" * 100, "with inner space"])
def test_valid_name_is_accepted(clean_db: None, app_conn: psycopg.Connection, name: str) -> None:
    app_conn.execute(INSERT, (name, "USD", False))


@pytest.mark.parametrize(
    "name",
    ["", "x" * 101],
    ids=["empty", "101 characters"],
)
def test_name_must_be_1_to_100_characters(
    clean_db: None, app_conn: psycopg.Connection, name: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(INSERT, (name, "USD", False))


@pytest.mark.parametrize(
    "name",
    [
        " alice",
        "alice ",
        "\talice",
        "alice\t",
        "\nalice",
        "alice\n",
        "\x0balice",
        "alice\x0b",
        "\x0calice",
        "alice\x0c",
        "\ralice",
        "alice\r",
        " ",
    ],
    ids=repr,
)
def test_name_must_not_have_leading_or_trailing_whitespace(
    clean_db: None, app_conn: psycopg.Connection, name: str
) -> None:
    """Rejected, not stripped, so what was sent is exactly what is stored.

    Whitespace means the six ASCII whitespace characters: space, tab, newline, vertical tab,
    form feed and carriage return.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(INSERT, (name, "USD", False))


@pytest.mark.parametrize(
    "name",
    ["alice ", " alice", "alice ", "　alice"],
    ids=["trailing NBSP", "leading NBSP", "trailing em space", "leading ideographic space"],
)
def test_unicode_whitespace_is_not_treated_as_whitespace(
    clean_db: None, app_conn: psycopg.Connection, name: str
) -> None:
    """Pins the decided rule: only ASCII whitespace is rejected at the ends of a name.

    Names are compared exactly, so a name with a Unicode space is simply a different name.
    """
    app_conn.execute(INSERT, (name, "USD", False))


# --- migration 005 renamed accounts.name to account_name ("name" is a keyword SQLFluff flags) ---


def migration_number(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def test_the_name_column_is_called_account_name(owner_conn: psycopg.Connection) -> None:
    rows = owner_conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'accounts'"
    ).fetchall()
    columns = {r[0] for r in rows}
    assert "account_name" in columns
    assert "name" not in columns


def test_the_name_constraints_follow_the_column_name(owner_conn: psycopg.Connection) -> None:
    rows = owner_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'public.accounts'::regclass"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {
        "accounts_account_name_key",
        "accounts_account_name_length",
        "accounts_account_name_whitespace"
    } <= names
    assert not {n for n in names if n.startswith("accounts_name_")}


def test_the_unique_name_index_follows_the_constraint_name(owner_conn: psycopg.Connection) -> None:
    rows = owner_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'accounts'"
    ).fetchall()
    indexes = {r[0] for r in rows}
    assert "accounts_account_name_key" in indexes
    assert "accounts_name_key" not in indexes


def test_migration_005_keeps_existing_accounts_and_their_rules(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """The rename runs on a database that already has accounts: the rows must survive under the
    new column name, and the unique and CHECK rules must still apply, under their new names."""
    migrations = sorted(settings.migrations_dir.glob("*.sql"))
    before = [m for m in migrations if migration_number(m) < 5]
    rename = [m for m in migrations if migration_number(m) == 5]
    assert len(before) == 4, "expected migrations 001 to 004 to exist"
    assert len(rename) == 1, "the rename migration (005) does not exist yet"

    for m in before:
        (tmp_path / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    run_migrations(scratch_url, tmp_path)
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO accounts (name, currency, allow_negative)"  # still "name" before 005
            " VALUES ('customer:alice', 'USD', false), ('funding:usd', 'USD', true)"
        )

    rename_file = rename[0]
    (tmp_path / rename_file.name).write_text(
        rename_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert run_migrations(scratch_url, tmp_path) == [rename_file.name]

    insert = (
        "INSERT INTO accounts (account_name, currency, allow_negative) VALUES (%s, 'EUR', false)"
    )
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT id, account_name, currency, allow_negative FROM accounts ORDER BY id"
        ).fetchall()
        assert rows == [(1, "customer:alice", "USD", False), (2, "funding:usd", "USD", True)]

        for value, error, constraint in [
            ("customer:alice", psycopg.errors.UniqueViolation, "accounts_account_name_key"),
            ("x" * 101, psycopg.errors.CheckViolation, "accounts_account_name_length"),
            (" padded", psycopg.errors.CheckViolation, "accounts_account_name_whitespace"),
        ]:
            with pytest.raises(error) as failure:
                conn.execute(insert, (value,))
            assert failure.value.diag.constraint_name == constraint
