"""Properties of the balances table (docs/design.md, Tables) checked through the app role.

balances is a cache of the entries. Its constraints are the safety net behind the transfer code:
a protected account (allow_negative false) can never hold a negative balance, and a row's
allow_negative can never disagree with its account's.
"""

from pathlib import Path

import psycopg
import pytest

from ledger.config import Settings
from ledger.migrate import run_migrations

BIGINT_MAX = 9_223_372_036_854_775_807

INSERT_BALANCE = (
    "INSERT INTO balances (account_id, allow_negative, balance_minor) VALUES (%s, %s, %s)"
)


def insert_account(
    conn: psycopg.Connection,
    name: str = "customer:alice",
    currency: str = "USD",
    allow_negative: bool = False,
) -> int:
    row = conn.execute(
        "INSERT INTO accounts (name, currency, allow_negative) VALUES (%s, %s, %s) RETURNING id",
        (name, currency, allow_negative),
    ).fetchone()
    assert row is not None
    return row[0]


def balance_of(conn: psycopg.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_balance_is_a_not_null_bigint(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'balances' AND column_name = 'balance_minor'"
    ).fetchone()
    assert row == ("bigint", "NO")


@pytest.mark.parametrize("allow_negative", [False, True])
def test_balance_row_can_be_created_for_an_existing_account(
    clean_db: None, app_conn: psycopg.Connection, allow_negative: bool
) -> None:
    account = insert_account(app_conn, allow_negative=allow_negative)
    app_conn.execute(INSERT_BALANCE, (account, allow_negative, 0))
    assert balance_of(app_conn, account) == 0


def test_account_id_is_the_primary_key(clean_db: None, app_conn: psycopg.Connection) -> None:
    """One balances row per account: this is the row every writer locks."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(INSERT_BALANCE, (account, False, 0))


def test_balance_row_must_reference_an_existing_account(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(INSERT_BALANCE, (999_999, False, 0))


@pytest.mark.parametrize("account_allows_negative", [False, True])
def test_balance_allow_negative_must_match_the_accounts(
    clean_db: None, app_conn: psycopg.Connection, account_allows_negative: bool
) -> None:
    """The copy of allow_negative cannot drift from the account's, in either direction."""
    account = insert_account(app_conn, allow_negative=account_allows_negative)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(INSERT_BALANCE, (account, not account_allows_negative, 0))


def test_allow_negative_cannot_be_changed_on_an_existing_row(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """The app role can UPDATE balances, so the foreign key stops allow_negative drifting."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(
            "UPDATE balances SET allow_negative = true WHERE account_id = %s", (account,)
        )


@pytest.mark.parametrize(
    "values",
    [(False, None), (None, 0)],
    ids=["balance_minor", "allow_negative"],
)
def test_columns_are_not_null(
    clean_db: None, app_conn: psycopg.Connection, values: tuple[bool | None, int | None]
) -> None:
    """A NULL allow_negative would skip the foreign key (the default MATCH SIMPLE) and pass the
    CHECK (NULL OR ...), so a protected account could go negative through it."""
    account = insert_account(app_conn)
    allow_negative, balance = values
    with pytest.raises(psycopg.errors.NotNullViolation):
        app_conn.execute(INSERT_BALANCE, (account, allow_negative, balance))


@pytest.mark.parametrize("balance", [0, 1, 99_900_000_000, BIGINT_MAX])
def test_protected_account_can_hold_zero_or_a_positive_balance(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, balance))
    assert balance_of(app_conn, account) == balance


@pytest.mark.parametrize("balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_protected_account_cannot_start_negative(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    account = insert_account(app_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(INSERT_BALANCE, (account, False, balance))


@pytest.mark.parametrize("new_balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_update_cannot_push_a_protected_account_below_zero(
    clean_db: None, app_conn: psycopg.Connection, new_balance: int
) -> None:
    """Invariant: no protected account goes negative. The transfer code checks first; this CHECK
    is the backstop that makes a bug there impossible to commit."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 5_000))
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(
            "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (new_balance, account)
        )


def test_protected_balance_can_be_updated_to_another_non_negative_value(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 5_000))
    app_conn.execute("UPDATE balances SET balance_minor = 0 WHERE account_id = %s", (account,))
    assert balance_of(app_conn, account) == 0


@pytest.mark.parametrize("balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_funding_account_may_go_negative(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    """Funding accounts issue money, so they go negative by exactly what they have issued."""
    funding = insert_account(app_conn, "funding:usd", allow_negative=True)
    app_conn.execute(INSERT_BALANCE, (funding, True, 0))
    app_conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (balance, funding)
    )
    assert balance_of(app_conn, funding) == balance


@pytest.mark.parametrize("statement", ["DELETE FROM balances", "TRUNCATE balances"])
def test_balances_cannot_be_deleted_by_the_app_role(
    clean_db: None, app_conn: psycopg.Connection, statement: str
) -> None:
    """Every account must keep its row, because every transfer locks it. Rows are never removed."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    app_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)


def test_migration_backfills_a_row_for_every_existing_account(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """The balances migration runs on a database that already has accounts and entries, so it must
    build each row from the entries and copy the account's allow_negative.

    Applies migrations 001 to 003, inserts data, then applies the balances migration (004).
    """
    migrations = sorted(settings.migrations_dir.glob("*.sql"))
    before = [m for m in migrations if int(m.name.split("_", 1)[0]) < 4]
    balances = [m for m in migrations if int(m.name.split("_", 1)[0]) == 4]
    assert len(before) == 3, "expected migrations 001 to 003 to exist"
    assert len(balances) == 1, "the balances migration (004) does not exist yet"

    for m in before:
        (tmp_path / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    run_migrations(scratch_url, tmp_path)

    with psycopg.connect(scratch_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO accounts (name, currency, allow_negative) VALUES"
            " ('customer:alice', 'USD', false),"  # id 1: receives and spends
            " ('customer:bob', 'USD', false),"  # id 2: has no entries
            " ('funding:usd', 'USD', true)"  # id 3: issues the money
        )
        conn.execute(
            "INSERT INTO transactions (idempotency_key, request_hash)"
            " VALUES ('k1', 'h'), ('k2', 'h')"
        )
        conn.execute(
            "INSERT INTO entries (transaction_id, account_id, amount_minor, currency) VALUES"
            " (1, 3, -5000, 'USD'), (1, 1, 5000, 'USD'),"  # fund alice with 5000
            " (2, 1, -1200, 'USD'), (2, 3, 1200, 'USD')"  # alice pays 1200 back
        )

    (tmp_path / balances[0].name).write_text(balances[0].read_text(encoding="utf-8"), "utf-8")
    assert run_migrations(scratch_url, tmp_path) == [balances[0].name]

    with psycopg.connect(scratch_url) as conn:
        rows = conn.execute(
            "SELECT account_id, allow_negative, balance_minor FROM balances ORDER BY account_id"
        ).fetchall()
    assert rows == [
        (1, False, 3800),  # 5000 - 1200
        (2, False, 0),  # no entries at all
        (3, True, -3800),  # the funding account is negative by what it issued
    ]
