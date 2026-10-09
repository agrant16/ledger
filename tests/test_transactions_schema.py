"""Properties of the transactions table (docs/design.md, Tables) checked through the app role."""

import psycopg
import pytest

HASH = "a" * 64

INSERT = (
    "INSERT INTO transactions (idempotency_key, request_hash, reverses_transaction_id)"
    " VALUES (%s, %s, %s) RETURNING id"
)


def insert_transaction(
    conn: psycopg.Connection, key: str = "key-1", reverses: int | None = None
) -> int:
    row = conn.execute(INSERT, (key, HASH, reverses)).fetchone()
    assert row is not None
    return row[0]


def test_ids_are_database_assigned_bigint_identity(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type, is_identity, identity_generation FROM information_schema.columns"
        " WHERE table_name = 'transactions' AND column_name = 'id'"
    ).fetchone()
    assert row == ("bigint", "YES", "ALWAYS")


def test_transaction_can_be_created_and_gets_an_id(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    first = insert_transaction(app_conn, "key-1")
    second = insert_transaction(app_conn, "key-2")
    assert first >= 1
    assert second > first


def test_caller_cannot_choose_the_id(clean_db: None, app_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.GeneratedAlways):
        app_conn.execute(
            "INSERT INTO transactions (id, idempotency_key, request_hash) VALUES (7, 'k', 'h')"
        )


def test_created_at_defaults_to_now(clean_db: None, app_conn: psycopg.Connection) -> None:
    tx_id = insert_transaction(app_conn)
    row = app_conn.execute(
        "SELECT created_at <= now() AND created_at > now() - interval '1 minute'"
        " FROM transactions WHERE id = %s",
        (tx_id,),
    ).fetchone()
    assert row == (True,)


def test_a_normal_transfer_reverses_nothing(clean_db: None, app_conn: psycopg.Connection) -> None:
    tx_id = insert_transaction(app_conn)
    row = app_conn.execute(
        "SELECT reverses_transaction_id FROM transactions WHERE id = %s", (tx_id,)
    ).fetchone()
    assert row == (None,)


def test_idempotency_key_is_unique(clean_db: None, app_conn: psycopg.Connection) -> None:
    insert_transaction(app_conn, "same-key")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_transaction(app_conn, "same-key")


@pytest.mark.parametrize("key", ["k", "x" * 255, "with spaces and symbols !@#"])
def test_valid_idempotency_key_is_accepted(
    clean_db: None, app_conn: psycopg.Connection, key: str
) -> None:
    insert_transaction(app_conn, key)


@pytest.mark.parametrize("key", ["", "x" * 256], ids=["empty", "256 characters"])
def test_idempotency_key_must_be_1_to_255_characters(
    clean_db: None, app_conn: psycopg.Connection, key: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_transaction(app_conn, key)


@pytest.mark.parametrize(
    "values",
    [(None, HASH), ("key-1", None)],
    ids=["idempotency_key", "request_hash"],
)
def test_columns_are_not_null(
    clean_db: None, app_conn: psycopg.Connection, values: tuple[str | None, str | None]
) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        app_conn.execute(INSERT, (*values, None))


def test_a_reversal_can_point_at_an_existing_transaction(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    original = insert_transaction(app_conn, "original")
    reversal = insert_transaction(app_conn, "reversal", reverses=original)
    row = app_conn.execute(
        "SELECT reverses_transaction_id FROM transactions WHERE id = %s", (reversal,)
    ).fetchone()
    assert row == (original,)


def test_a_reversal_cannot_point_at_a_missing_transaction(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_transaction(app_conn, "reversal", reverses=999_999)


def test_a_transaction_can_be_reversed_only_once(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    original = insert_transaction(app_conn, "original")
    insert_transaction(app_conn, "first-reversal", reverses=original)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_transaction(app_conn, "second-reversal", reverses=original)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET request_hash = 'changed'",
        "UPDATE transactions SET reverses_transaction_id = id",
        "UPDATE transactions SET idempotency_key = 'changed'",
        "DELETE FROM transactions",
        "TRUNCATE transactions",
    ],
)
def test_transactions_are_immutable_to_the_app_role(
    clean_db: None, app_conn: psycopg.Connection, statement: str
) -> None:
    """request_hash and reverses_transaction_id cannot change after the fact."""
    insert_transaction(app_conn)
    app_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)
