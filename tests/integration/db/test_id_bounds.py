"""Row ids are positive (docs/design.md, Id bounds; ADR 0008).

`id BIGINT GENERATED ALWAYS AS IDENTITY` keeps ids out of the caller's hands, but not completely:
any role that may insert can add OVERRIDING SYSTEM VALUE and choose the id, and Postgres has no
privilege that forbids it. The sequences themselves never produce a zero or a negative number.
A CHECK on each generated id column makes the lower bound a database rule, matching the API, which
accepts only ids from 1 to 9,223,372,036,854,775,807.

    accounts_id_positive, transactions_id_positive, entries_id_positive: CHECK (id > 0)

Foreign key columns (account_id, transaction_id, reverses_transaction_id) can only point at rows
that exist, so they need no check of their own.
"""

import psycopg
import pytest

from tests.db_helpers import VALID_HASH

BIGINT_MAX = 9_223_372_036_854_775_807
BIGINT_MIN = -9_223_372_036_854_775_808

TABLES = ["accounts", "transactions", "entries"]

NOT_POSITIVE = [0, -1, BIGINT_MIN]
NOT_POSITIVE_IDS = ["zero", "minus one", "bigint minimum"]
POSITIVE = [1, BIGINT_MAX]
POSITIVE_IDS = ["one", "bigint maximum"]


def insert_with_id(conn: psycopg.Connection, table: str, row_id: int) -> None:
    """Insert one row into the table, choosing its id with OVERRIDING SYSTEM VALUE. An entry needs
    an account and a transaction first; those get ordinary generated ids. Plain SQL throughout, so
    it works on the owner connection too (the service functions refuse an autocommit connection)."""
    if table == "accounts":
        conn.execute(
            "INSERT INTO accounts (id, account_name, currency, allow_negative)"
            " OVERRIDING SYSTEM VALUE VALUES (%s, 'customer:alice', 'USD', false)",
            (row_id,),
        )
    elif table == "transactions":
        conn.execute(
            "INSERT INTO transactions (id, idempotency_key, request_hash)"
            " OVERRIDING SYSTEM VALUE VALUES (%s, 'key-1', %s)",
            (row_id, VALID_HASH),
        )
    else:
        account = conn.execute(
            "INSERT INTO accounts (account_name, currency, allow_negative)"
            " VALUES ('customer:alice', 'USD', false) RETURNING id"
        ).fetchone()
        assert account is not None
        transaction = conn.execute(
            "INSERT INTO transactions (idempotency_key, request_hash) VALUES ('key-1', %s)"
            " RETURNING id",
            (VALID_HASH,),
        ).fetchone()
        assert transaction is not None
        conn.execute(
            "INSERT INTO entries (id, transaction_id, account_id, amount_minor, currency)"
            " OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, 100, 'USD')",
            (row_id, transaction[0], account[0]),
        )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("row_id", NOT_POSITIVE, ids=NOT_POSITIVE_IDS)
def test_the_app_role_cannot_choose_an_id_that_is_not_positive(
    clean_db: None, app_conn: psycopg.Connection, table: str, row_id: int
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        insert_with_id(app_conn, table, row_id)

    assert failure.value.diag.constraint_name == f"{table}_id_positive"


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("row_id", NOT_POSITIVE, ids=NOT_POSITIVE_IDS)
def test_the_owner_role_cannot_either(
    clean_db: None, owner_conn: psycopg.Connection, table: str, row_id: int
) -> None:
    """The owner connection is in autocommit mode, so the attempt runs in an explicit transaction
    that is always rolled back, even if the insert wrongly succeeds."""
    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        with owner_conn.transaction(force_rollback=True):
            insert_with_id(owner_conn, table, row_id)

    assert failure.value.diag.constraint_name == f"{table}_id_positive"


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("row_id", POSITIVE, ids=POSITIVE_IDS)
def test_a_chosen_id_from_one_to_the_bigint_maximum_is_accepted(
    clean_db: None, app_conn: psycopg.Connection, table: str, row_id: int
) -> None:
    insert_with_id(app_conn, table, row_id)

    row = app_conn.execute(f"SELECT count(*) FROM {table} WHERE id = %s", (row_id,)).fetchone()
    assert row == (1,)


@pytest.mark.parametrize("table", TABLES)
def test_generated_ids_are_positive_and_the_sequence_never_wraps(
    owner_conn: psycopg.Connection, table: str
) -> None:
    """What the sequences do by themselves: start at 1, count up by 1, stop at the bigint maximum
    with an error instead of wrapping around to a negative number."""
    row = owner_conn.execute(
        "SELECT s.seqstart, s.seqmin, s.seqmax, s.seqincrement, s.seqcycle"
        " FROM pg_sequence s WHERE s.seqrelid = pg_get_serial_sequence(%s, 'id')::regclass",
        (table,),
    ).fetchone()
    assert row == (1, 1, BIGINT_MAX, 1, False)
