"""Properties of the entries table (docs/design.md, Tables) checked through the app role."""

import psycopg
import pytest

HASH = "a" * 64
BIGINT_MAX = 9_223_372_036_854_775_807

INSERT = (
    "INSERT INTO entries (transaction_id, account_id, amount_minor, currency)"
    " VALUES (%s, %s, %s, %s) RETURNING id"
)


def insert_account(
    conn: psycopg.Connection, name: str = "customer:alice", currency: str = "USD"
) -> int:
    row = conn.execute(
        "INSERT INTO accounts (name, currency, allow_negative) VALUES (%s, %s, false) RETURNING id",
        (name, currency),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_transaction(conn: psycopg.Connection, key: str = "key-1") -> int:
    row = conn.execute(
        "INSERT INTO transactions (idempotency_key, request_hash) VALUES (%s, %s) RETURNING id",
        (key, HASH),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_entry(
    conn: psycopg.Connection,
    transaction_id: int,
    account_id: int,
    amount: int,
    currency: str = "USD",
) -> int:
    row = conn.execute(INSERT, (transaction_id, account_id, amount, currency)).fetchone()
    assert row is not None
    return row[0]


def index_column_lists(conn: psycopg.Connection) -> list[tuple[str, ...]]:
    """Column names of every index on entries, in index order."""
    rows = conn.execute(
        "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_index i"
        " CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum"
        " WHERE i.indrelid = 'public.entries'::regclass"
        " GROUP BY i.indexrelid"
    ).fetchall()
    return [tuple(r[0]) for r in rows]


def test_ids_are_database_assigned_bigint_identity(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type, is_identity, identity_generation FROM information_schema.columns"
        " WHERE table_name = 'entries' AND column_name = 'id'"
    ).fetchone()
    assert row == ("bigint", "YES", "ALWAYS")


def test_amounts_are_signed_bigint_minor_units(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_name = 'entries' AND column_name = 'amount_minor'"
    ).fetchone()
    assert row == ("bigint",)


def test_entries_can_be_created_and_get_increasing_ids(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    first = insert_entry(app_conn, tx, account, -2500)
    second = insert_entry(app_conn, tx, account, 2500)
    assert first >= 1
    assert second > first


def test_caller_cannot_choose_the_id(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    with pytest.raises(psycopg.errors.GeneratedAlways):
        app_conn.execute(
            "INSERT INTO entries (id, transaction_id, account_id, amount_minor, currency)"
            " VALUES (7, %s, %s, 100, 'USD')",
            (tx, account),
        )


@pytest.mark.parametrize(
    "amount",
    [1, -1, 99_900_000_000, -99_900_000_000, BIGINT_MAX, -BIGINT_MAX],
)
def test_nonzero_amounts_of_either_sign_are_accepted(
    clean_db: None, app_conn: psycopg.Connection, amount: int
) -> None:
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    insert_entry(app_conn, tx, account, amount)


def test_zero_amount_is_rejected(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_entry(app_conn, tx, account, 0)


@pytest.mark.parametrize("column", ["transaction_id", "account_id", "amount_minor", "currency"])
def test_columns_are_not_null(clean_db: None, app_conn: psycopg.Connection, column: str) -> None:
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    values: dict[str, int | str | None] = {
        "transaction_id": tx,
        "account_id": account,
        "amount_minor": 100,
        "currency": "USD",
    }
    values[column] = None
    with pytest.raises(psycopg.errors.NotNullViolation):
        app_conn.execute(INSERT, tuple(values.values()))


def test_entry_currency_must_match_its_accounts_currency(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """Invariant: every entry's currency matches its account's currency."""
    usd_account = insert_account(app_conn, "customer:alice", "USD")
    tx = insert_transaction(app_conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_entry(app_conn, tx, usd_account, 100, currency="EUR")


def test_entry_must_reference_an_existing_account(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    tx = insert_transaction(app_conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_entry(app_conn, tx, 999_999, 100)


def test_entry_must_reference_an_existing_transaction(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    account = insert_account(app_conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_entry(app_conn, 999_999, account, 100)


def test_transaction_lookups_are_indexed(owner_conn: psycopg.Connection) -> None:
    """Postgres does not index foreign key columns. Every transaction lookup filters on
    entries.transaction_id (GET /transfers/{id}, replays, the zero-sum check)."""
    assert any(cols[:1] == ("transaction_id",) for cols in index_column_lists(owner_conn))


def test_account_history_pagination_is_indexed(owner_conn: psycopg.Connection) -> None:
    """Cursor pagination on entries.id within one account needs (account_id, id)."""
    assert any(cols[:2] == ("account_id", "id") for cols in index_column_lists(owner_conn))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE entries SET amount_minor = 1",
        "UPDATE entries SET currency = 'EUR'",
        "UPDATE entries SET account_id = account_id",
        "UPDATE entries SET transaction_id = transaction_id",
        "DELETE FROM entries",
        "TRUNCATE entries",
    ],
)
def test_entries_are_append_only_for_the_app_role(
    clean_db: None, app_conn: psycopg.Connection, statement: str
) -> None:
    """Invariant: entries are never changed after they are written. Corrections are new entries."""
    account = insert_account(app_conn)
    tx = insert_transaction(app_conn)
    insert_entry(app_conn, tx, account, 100)
    app_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)
