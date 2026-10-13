"""get_balance (ledger/accounts.py; docs/design.md, GET /accounts/{id}/balance).

    get_balance(conn, account_id) -> Balance(account_id, currency, balance_minor)

It reads the cached balance with one plain statement that joins `balances` to `accounts`, takes no
row locks, and raises UnknownAccountError when there is no such account. It does not change
anything, and the caller owns the transaction.
"""

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.accounts import create_account
from ledger.errors import UnknownAccountError


@pytest.fixture
def get_balance():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.accounts import get_balance

    return get_balance


def set_cached_balance(conn: psycopg.Connection, account_id: int, balance_minor: int) -> None:
    conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (balance_minor, account_id)
    )


def test_a_new_account_has_a_zero_balance_in_its_currency(
    clean_db: None, app_conn: psycopg.Connection, get_balance
) -> None:
    from ledger.accounts import Balance

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

    assert "999999" in str(failure.value)


def test_it_takes_no_row_locks(
    clean_db: None,
    app_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    commit_setup,
    get_balance,
) -> None:
    """Another connection holds the balances row FOR UPDATE, as a transfer in flight would. A
    plain read must not wait for it: the reading connection refuses to wait more than 300 ms."""
    account = create_account(app_conn, "customer:alice", "USD", False)
    commit_setup()

    with app_pool.connection() as holder:
        holder.execute("SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE", (account.id,))

        app_conn.execute("SET LOCAL lock_timeout = '300ms'")
        balance = get_balance(app_conn, account.id)

    assert balance.balance_minor == 0


def test_it_changes_nothing(clean_db: None, app_conn: psycopg.Connection, get_balance) -> None:
    account = create_account(app_conn, "customer:alice", "USD", False)
    set_cached_balance(app_conn, account.id, 12)

    get_balance(app_conn, account.id)
    get_balance(app_conn, account.id)

    row = app_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account.id,)
    ).fetchone()
    assert row == (12,)
