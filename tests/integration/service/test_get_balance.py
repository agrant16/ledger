"""get_balance (ledger/service/account_service.py; docs/design.md, GET /accounts/{id}/balance).

    get_balance(conn, account_id) -> Balance(account_id, currency, balance_minor)

The service reads the cached balance through the repository (`account_repo.find_balance`) and
decides what the answer means: no such account is UnknownAccountError (a LedgerError), and an
account that has no balances row is a bug (LedgerInvariantError), because create_account always
writes both. It changes nothing, and the caller owns the transaction. The SQL itself (one plain
statement, no row locks) is tested in tests/integration/db/test_account_repo.py.
"""

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError, UnknownAccountError
from ledger.infra.config import Settings
from ledger.models.account_model import Balance
from ledger.service.account_service import create_account


@pytest.fixture
def get_balance():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.service.account_service import get_balance

    return get_balance


def set_cached_balance(conn: psycopg.Connection, account_id: int, balance_minor: int) -> None:
    conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (balance_minor, account_id)
    )


def test_a_new_account_has_a_zero_balance_in_its_currency(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    account = create_account(app_conn, "customer:alice", "EUR", False)

    assert get_balance(app_conn, account.id) == Balance(account.id, "EUR", 0)


def test_it_returns_the_cached_balance_and_not_the_sum_of_the_entries(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    """There are no entries at all here, so a balance of 500 can only come from the cache."""
    account = create_account(app_conn, "customer:alice", "USD", False)
    set_cached_balance(app_conn, account.id, 500)

    assert get_balance(app_conn, account.id).balance_minor == 500


def test_a_funding_account_can_show_a_negative_balance(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    funding = create_account(app_conn, "funding:usd", "USD", True)
    set_cached_balance(app_conn, funding.id, -300)

    assert get_balance(app_conn, funding.id).balance_minor == -300


def test_each_account_gets_its_own_balance(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    alice = create_account(app_conn, "customer:alice", "USD", False)
    bob = create_account(app_conn, "customer:bob", "USD", False)
    set_cached_balance(app_conn, alice.id, 70)
    set_cached_balance(app_conn, bob.id, 80)

    assert get_balance(app_conn, alice.id).balance_minor == 70
    assert get_balance(app_conn, bob.id).balance_minor == 80


def test_an_unknown_account_raises_unknown_account_error(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    with pytest.raises(UnknownAccountError) as failure:
        get_balance(app_conn, 999_999)

    assert isinstance(failure.value, LedgerError)
    assert "999999" in str(failure.value)


def test_an_account_with_no_balances_row_is_a_bug_and_not_a_balance(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    """create_account always writes the balances row with the account, so an account that has none
    means the data is damaged. That is a bug (a logged 500), not an unknown account (404), and it
    must never come back as a Balance full of None."""
    row = app_conn.execute(
        "INSERT INTO accounts (account_name, currency, allow_negative)"
        " VALUES ('orphan', 'USD', false) RETURNING id"
    ).fetchone()
    assert row is not None

    with pytest.raises(LedgerInvariantError) as failure:
        get_balance(app_conn, row[0])

    assert not isinstance(failure.value, LedgerError)


def test_it_refuses_an_autocommit_connection(
    clean_db: None, settings: Settings, get_balance
) -> None:
    """Every service function that is given a connection assumes a transaction is in progress."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            get_balance(conn, 1)


# --- the decisions, with the repository answering what the test says ---------------------------


@pytest.fixture
def account_repo():
    from ledger.repository import account_repo

    return account_repo


def test_the_service_asks_the_repository_and_returns_its_balance(
    app_conn: psycopg.Connection, get_balance, account_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = []

    def find_balance(conn, account_id):
        asked.append((conn, account_id))
        return Balance(account_id, "EUR", 42)

    monkeypatch.setattr(account_repo, "find_balance", find_balance)

    assert get_balance(app_conn, 7) == Balance(7, "EUR", 42)
    assert asked == [(app_conn, 7)]


def test_nothing_found_means_an_unknown_account(
    app_conn: psycopg.Connection, get_balance, account_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(account_repo, "find_balance", lambda conn, account_id: None)

    with pytest.raises(UnknownAccountError):
        get_balance(app_conn, 7)


def test_an_account_without_a_balance_is_a_bug_whatever_the_repository_class_is(
    app_conn: psycopg.Connection, get_balance, account_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        account_repo, "find_balance", lambda conn, account_id: Balance(account_id, "USD", None)
    )

    with pytest.raises(LedgerInvariantError):
        get_balance(app_conn, 7)
