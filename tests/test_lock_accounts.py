"""_lock_accounts (docs/design.md, milestone 2): lock the touched balances rows and read them.

    _lock_accounts(conn, account_ids) -> dict[int, LockedAccount]

It takes the ids an entry list touches and runs one locking statement over them, returning for
each known account its currency, whether it may go negative, and its balance as read after the lock
was taken. post_transaction checks the entries against that result, so the overdraft check can never
read a stale balance. The result is keyed by account id in ascending order. An id with no account
is left out of the result: deciding that it is an error (UnknownAccountError) is
post_transaction's job.

The app role has no UPDATE privilege on accounts, so the statement can only lock balances rows.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.accounts import Account, create_account


@pytest.fixture
def api():
    """Imported lazily so these tests fail one by one while the helper does not exist yet."""
    from ledger import posting

    return posting


def make(
    conn: psycopg.Connection, name: str, currency: str = "USD", allow_negative: bool = False
) -> Account:
    return create_account(conn, name, currency, allow_negative)


def set_balance(conn: psycopg.Connection, account: Account, balance_minor: int) -> None:
    """Set the cached balance directly. These tests are about locking and reading, not posting."""
    conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s",
        (balance_minor, account.id),
    )


LOCK_NOWAIT = "SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE NOWAIT"


def scan_in_storage_order(conn: psycopg.Connection) -> None:
    """Make this transaction's queries scan tables in storage order, not through the primary key.

    Without it the planner can return rows in id order by accident, and a statement missing its
    ORDER BY would pass the ordering tests."""
    for setting in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
        conn.execute(f"SET LOCAL {setting} = off")


def is_locked(pool: ConnectionPool, account: Account) -> bool:
    """True if another connection cannot lock the account's balances row right now."""
    with pool.connection() as other:
        try:
            other.execute(LOCK_NOWAIT, (account.id,))
        except psycopg.errors.LockNotAvailable:
            return True
        finally:
            other.rollback()
        return False


def wait_until_a_session_is_blocked_on_a_lock(owner_conn: psycopg.Connection) -> None:
    """Poll until some lock request is waiting, so the test knows the thread got stuck on it."""
    for _ in range(100):
        row = owner_conn.execute("SELECT count(*) FROM pg_locks WHERE NOT granted").fetchone()
        assert row is not None
        if row[0] >= 1:
            return
        time.sleep(0.05)
    pytest.fail("no session ever blocked on a lock")


# --- what it returns --------------------------------------------------------------------------


def test_returns_each_accounts_currency_overdraft_flag_and_balance(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice", "USD")
    funding = make(app_conn, "funding:eur", "EUR", allow_negative=True)
    set_balance(app_conn, alice, 2500)
    set_balance(app_conn, funding, -900)

    result = api._lock_accounts(app_conn, [alice.id, funding.id])

    assert result == {
        alice.id: api.LockedAccount(currency="USD", allow_negative=False, balance_minor=2500),
        funding.id: api.LockedAccount(currency="EUR", allow_negative=True, balance_minor=-900),
    }


def test_the_result_is_keyed_in_ascending_account_id_order(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    first = make(app_conn, "customer:first")
    second = make(app_conn, "customer:second")
    third = make(app_conn, "customer:third")
    set_balance(app_conn, first, 1)  # its newest row version now sits after the others in storage
    scan_in_storage_order(app_conn)
    result = api._lock_accounts(app_conn, [third.id, first.id, second.id])
    assert list(result) == [first.id, second.id, third.id]


def test_an_unknown_account_is_left_out_and_not_an_error(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    result = api._lock_accounts(app_conn, [alice.id, 999_999])
    assert list(result) == [alice.id]


def test_an_id_given_twice_is_returned_once(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    result = api._lock_accounts(app_conn, [alice.id, alice.id])
    assert list(result) == [alice.id]


def test_no_ids_returns_nothing(clean_db: None, app_conn: psycopg.Connection, api) -> None:
    make(app_conn, "customer:alice")
    assert api._lock_accounts(app_conn, []) == {}


# --- what it locks ----------------------------------------------------------------------------


def test_it_locks_the_requested_rows_and_only_those(
    clean_db: None,
    app_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    commit_setup,
    api,
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    carol = make(app_conn, "customer:carol")
    commit_setup()

    api._lock_accounts(app_conn, [alice.id, bob.id, 999_999])

    assert is_locked(app_pool, alice)
    assert is_locked(app_pool, bob)
    assert not is_locked(app_pool, carol)


# --- the order the locks are taken in ----------------------------------------------------------


def test_locks_are_taken_in_ascending_account_id_whatever_order_it_is_asked_in(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    commit_setup,
    api,
) -> None:
    """Ascending order is what makes two transfers between the same accounts unable to deadlock.

    Another session holds the higher account's row, so a call asking for [high, low] must stop at
    high. If it locked in ascending order it already holds low by then; if it locked in the order it
    was given, it holds nothing. The low account's row is updated once after both are created, so
    its newest version sits after high's in the table: a statement with no ORDER BY, scanning the
    table in storage order, would reach high first and fail this test."""
    low = make(app_conn, "customer:low")
    high = make(app_conn, "customer:high")
    set_balance(app_conn, low, 1)
    commit_setup()

    def ask_for_both():
        with app_pool.connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '10s'")
            scan_in_storage_order(conn)
            try:
                return api._lock_accounts(conn, [high.id, low.id])
            finally:
                conn.rollback()

    with app_pool.connection() as holder, ThreadPoolExecutor(max_workers=1) as executor:
        try:
            holder.execute("SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE", (high.id,))
            future = executor.submit(ask_for_both)
            wait_until_a_session_is_blocked_on_a_lock(owner_conn)
            low_is_already_held = is_locked(app_pool, low)
        finally:
            holder.rollback()
        result = future.result(timeout=15)

    assert low_is_already_held, "the lock on the lower account id should be taken first"
    assert list(result) == [low.id, high.id]


# --- the balance is read after the lock -------------------------------------------------------


def test_the_balance_is_read_after_waiting_for_the_lock(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    commit_setup,
    api,
) -> None:
    """READ COMMITTED: a locking SELECT that had to wait returns the row as the other writer left
    it, not as it was when the statement began. This is why the overdraft check can trust the
    balance it gets back."""
    alice = make(app_conn, "customer:alice")
    set_balance(app_conn, alice, 100)
    commit_setup()

    def ask():
        with app_pool.connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '10s'")
            try:
                return api._lock_accounts(conn, [alice.id])
            finally:
                conn.rollback()

    with app_pool.connection() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        try:
            set_balance(writer, alice, 700)  # takes the row lock; not yet committed
            future = executor.submit(ask)
            wait_until_a_session_is_blocked_on_a_lock(owner_conn)
            writer.commit()
        except BaseException:
            writer.rollback()
            raise
        result = future.result(timeout=15)

    assert result[alice.id].balance_minor == 700
