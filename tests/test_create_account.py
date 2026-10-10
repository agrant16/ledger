"""create_account (docs/design.md, milestone 2): the account and its balances row, together.

It takes the request's connection and never commits; the caller owns the transaction. The API
validates input, so the service does not: a bad value reaches the database CHECK and surfaces as
the raw error, which is a bug-class outcome.
"""

import psycopg
import pytest

from ledger.accounts import create_account
from ledger.config import Settings
from ledger.errors import DuplicateAccountNameError, LedgerInvariantError


@pytest.fixture
def failing_balances_insert(owner_conn: psycopg.Connection):
    """Make every INSERT into balances fail, to simulate a failure after the account row exists.

    A trigger created by the owner role, removed again however the test ends. It works whatever
    structure create_account has, because it fails the database operation itself.
    """
    owner_conn.execute(
        "CREATE FUNCTION probe_refuse_balances_insert() RETURNS trigger LANGUAGE plpgsql AS $$"
        " BEGIN RAISE EXCEPTION 'probe: balances insert refused'; END $$"
    )
    owner_conn.execute(
        "CREATE TRIGGER probe_refuse_balances_insert BEFORE INSERT ON balances"
        " FOR EACH ROW EXECUTE FUNCTION probe_refuse_balances_insert()"
    )
    try:
        yield
    finally:
        owner_conn.execute("DROP TRIGGER IF EXISTS probe_refuse_balances_insert ON balances")
        owner_conn.execute("DROP FUNCTION IF EXISTS probe_refuse_balances_insert()")


def count_accounts(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT count(*) FROM accounts WHERE name = %s", (name,)).fetchone()
    assert row is not None
    return row[0]


def test_returns_the_new_account(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = create_account(app_conn, "customer:alice", "USD", False)
    assert account.id >= 1
    assert (account.name, account.currency, account.allow_negative) == (
        "customer:alice",
        "USD",
        False,
    )


def test_writes_the_accounts_row(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = create_account(app_conn, "customer:alice", "EUR", False)
    row = app_conn.execute(
        "SELECT name, currency, allow_negative FROM accounts WHERE id = %s", (account.id,)
    ).fetchone()
    assert row == ("customer:alice", "EUR", False)


@pytest.mark.parametrize("allow_negative", [False, True])
def test_creates_a_zero_balance_row_copying_allow_negative(
    clean_db: None, app_conn: psycopg.Connection, allow_negative: bool
) -> None:
    """There must always be a balances row to lock, so it is created with the account."""
    account = create_account(app_conn, "customer:alice", "USD", allow_negative)
    row = app_conn.execute(
        "SELECT account_id, allow_negative, balance_minor FROM balances WHERE account_id = %s",
        (account.id,),
    ).fetchone()
    assert row == (account.id, allow_negative, 0)


def test_the_funding_account_fixture_makes_an_allow_negative_account(
    clean_db: None, app_conn: psycopg.Connection, make_funding_account
) -> None:
    usd = make_funding_account("USD")
    eur = make_funding_account("EUR")
    assert (usd.name, usd.currency, usd.allow_negative) == ("funding:USD", "USD", True)
    assert (eur.name, eur.currency, eur.allow_negative) == ("funding:EUR", "EUR", True)
    assert usd.id != eur.id
    rows = app_conn.execute(
        "SELECT account_id, allow_negative, balance_minor FROM balances ORDER BY account_id"
    ).fetchall()
    assert rows == [(usd.id, True, 0), (eur.id, True, 0)]


def test_a_duplicate_name_raises_duplicate_account_name_error(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """Names are unique across currencies, so the second call fails even with another currency."""
    create_account(app_conn, "customer:alice", "USD", False)
    with pytest.raises(DuplicateAccountNameError) as failure:
        with app_conn.transaction():  # a savepoint, so the first account survives the failure
            create_account(app_conn, "customer:alice", "EUR", False)
    assert failure.value.name == "customer:alice"


def test_a_failed_duplicate_leaves_nothing_behind(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    create_account(app_conn, "customer:alice", "USD", False)
    with pytest.raises(DuplicateAccountNameError):
        with app_conn.transaction():
            create_account(app_conn, "customer:alice", "EUR", False)
    assert count_accounts(app_conn, "customer:alice") == 1
    assert app_conn.execute("SELECT count(*) FROM balances").fetchone() == (1,)


def test_names_are_case_sensitive(clean_db: None, app_conn: psycopg.Connection) -> None:
    create_account(app_conn, "customer:alice", "USD", False)
    create_account(app_conn, "Customer:Alice", "USD", False)


def test_it_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
) -> None:
    """The caller owns the transaction, so a rollback must undo the account and its balance."""
    create_account(app_conn, "customer:alice", "USD", False)
    assert count_accounts(app_conn, "customer:alice") == 1  # it happened, so the rollback counts
    app_conn.rollback()
    assert count_accounts(owner_conn, "customer:alice") == 0
    assert owner_conn.execute("SELECT count(*) FROM balances").fetchone() == (0,)


def test_it_refuses_an_autocommit_connection(
    clean_db: None, settings: Settings, owner_conn: psycopg.Connection
) -> None:
    """On an autocommit connection each statement would commit on its own, so the account and its
    balances row could no longer succeed or fail together. That is a bug in the caller."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            create_account(conn, "customer:alice", "USD", False)
    assert count_accounts(owner_conn, "customer:alice") == 0


def test_a_bad_currency_surfaces_the_database_check(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """Validation belongs to the API layer. The service does not repeat it, so the raw database
    error comes through, and it is not a LedgerError."""
    with pytest.raises(psycopg.errors.CheckViolation):
        create_account(app_conn, "customer:alice", "usd", False)


def test_a_failure_creating_the_balances_row_leaves_no_account(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    failing_balances_insert: None,
) -> None:
    """The account and its balances row succeed or fail together. The raw error is not hidden."""
    with pytest.raises(psycopg.errors.RaiseException):
        create_account(app_conn, "customer:alice", "USD", False)
    app_conn.rollback()
    assert count_accounts(owner_conn, "customer:alice") == 0
