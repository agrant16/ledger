"""create_account (ledger/service/account_service.py; docs/design.md): the account and its balances
row, together.

It takes the request's connection and never commits; the caller owns the transaction. It writes the
two rows through the repository (`account_repo.insert_account`, then `account_repo.insert_balance`).
The API validates input, so the service does not: a bad value reaches the database CHECK, which the
repository reports as a LedgerInvariantError (a bug, never a LedgerError). A duplicate name is the
expected outcome, DuplicateAccountNameError. The SQL and the error translation themselves are tested
in tests/integration/db/test_account_repo.py.
"""

import psycopg
import pytest

from ledger.errors import DuplicateAccountNameError, LedgerError, LedgerInvariantError
from ledger.infra.config import Settings
from ledger.service.account_service import create_account


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
    row = conn.execute("SELECT count(*) FROM accounts WHERE account_name = %s", (name,)).fetchone()
    assert row is not None
    return row[0]


def test_returns_the_new_account(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = create_account(app_conn, "customer:alice", "USD", False)
    assert account.id >= 1
    assert (account.account_name, account.currency, account.allow_negative) == (
        "customer:alice",
        "USD",
        False,
    )


def test_writes_the_accounts_row(clean_db: None, app_conn: psycopg.Connection) -> None:
    account = create_account(app_conn, "customer:alice", "EUR", False)
    row = app_conn.execute(
        "SELECT account_name, currency, allow_negative FROM accounts WHERE id = %s",
        (account.id,),
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
    assert (usd.account_name, usd.currency, usd.allow_negative) == (
        "funding:USD",
        "USD",
        True,
    )
    assert (eur.account_name, eur.currency, eur.allow_negative) == (
        "funding:EUR",
        "EUR",
        True,
    )
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
    assert failure.value.account_name == "customer:alice"


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
    """Validation belongs to the API layer. The service does not repeat it, so the database CHECK
    fires, and the repository reports it as a bug that is not a LedgerError."""
    with pytest.raises(LedgerInvariantError) as failure:
        create_account(app_conn, "customer:alice", "usd", False)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)


def test_a_failure_creating_the_balances_row_leaves_no_account(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    failing_balances_insert: None,
) -> None:
    """The account and its balances row succeed or fail together. An error that is not a constraint
    violation is not hidden or translated."""
    with pytest.raises(psycopg.errors.RaiseException):
        create_account(app_conn, "customer:alice", "USD", False)
    app_conn.rollback()
    assert count_accounts(owner_conn, "customer:alice") == 0


def test_it_writes_the_account_and_then_its_balance_row_through_the_repository(
    clean_db: None, app_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.repository import account_repo

    calls = []
    real_insert_account = account_repo.insert_account
    real_insert_balance = account_repo.insert_balance

    def insert_account(conn, account_name, currency, allow_negative):
        calls.append(("insert_account", conn, account_name, currency, allow_negative))
        return real_insert_account(conn, account_name, currency, allow_negative)

    def insert_balance(conn, account_id, allow_negative):
        calls.append(("insert_balance", conn, account_id, allow_negative))
        return real_insert_balance(conn, account_id, allow_negative)

    monkeypatch.setattr(account_repo, "insert_account", insert_account)
    monkeypatch.setattr(account_repo, "insert_balance", insert_balance)

    account = create_account(app_conn, "funding:usd", "USD", True)

    assert calls == [
        ("insert_account", app_conn, "funding:usd", "USD", True),
        ("insert_balance", app_conn, account.id, True),
    ]


def test_a_duplicate_name_does_not_reach_the_balance_step(
    clean_db: None, app_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.repository import account_repo

    create_account(app_conn, "customer:alice", "USD", False)
    balance_inserts = []
    real_insert_balance = account_repo.insert_balance
    monkeypatch.setattr(
        account_repo,
        "insert_balance",
        lambda *args, **kwargs: (
            balance_inserts.append(args) or real_insert_balance(*args, **kwargs)
        ),
    )

    with pytest.raises(DuplicateAccountNameError):
        with app_conn.transaction():
            create_account(app_conn, "customer:alice", "EUR", False)

    assert balance_inserts == []
