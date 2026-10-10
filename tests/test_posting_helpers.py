"""_insert_entries and _apply_balance_deltas (docs/design.md, milestone 2): the two write steps.

post_transaction checks the entries and locks the balances rows first. These two helpers then do the
writing, and trust what they are given:

    _insert_entries(conn, transaction_id, entries) -> None
        one entries row per Entry, in list order, against the given transaction.
    _apply_balance_deltas(conn, deltas) -> None
        adds each delta to that account's cached balance.

Neither checks for an overdraft: that is post_transaction's job, using the locked balances, and the
balances CHECK constraint is only the safety net behind it. Neither commits; the caller owns the
transaction. They are tested here directly so a failure points at the helper, not at
post_transaction as a whole.
"""

import itertools

import psycopg
import pytest

from ledger.accounts import Account, create_account
from ledger.errors import LedgerError, LedgerInvariantError

KEYS = itertools.count(1)


@pytest.fixture
def api():
    """Imported lazily so these tests fail one by one while the helpers do not exist yet."""
    from ledger import posting

    return posting


def make(
    conn: psycopg.Connection, name: str, currency: str = "USD", allow_negative: bool = False
) -> Account:
    return create_account(conn, name, currency, allow_negative)


def claim(api, conn: psycopg.Connection) -> int:
    return api.claim_transaction(conn, f"key-{next(KEYS)}", "hash")


def set_balance(conn: psycopg.Connection, account: Account, balance_minor: int) -> None:
    conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s",
        (balance_minor, account.id),
    )


def balance(conn: psycopg.Connection, account: Account) -> int:
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account.id,)
    ).fetchone()
    assert row is not None
    return row[0]


def balances(conn: psycopg.Connection) -> dict[int, int]:
    return dict(conn.execute("SELECT account_id, balance_minor FROM balances").fetchall())


def entry_rows(conn: psycopg.Connection, tx: int) -> list[tuple[int, int, str]]:
    return conn.execute(
        "SELECT account_id, amount_minor, currency FROM entries"
        " WHERE transaction_id = %s ORDER BY id",
        (tx,),
    ).fetchall()


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


# --- _insert_entries --------------------------------------------------------------------------


def test_insert_writes_one_row_per_entry_in_list_order(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    carol = make(app_conn, "customer:carol")
    tx = claim(api, app_conn)
    entries = [
        api.Entry(bob.id, -300, "USD"),
        api.Entry(alice.id, 100, "USD"),
        api.Entry(carol.id, 200, "USD"),
    ]

    result = api._insert_entries(app_conn, tx, entries)

    assert result is None
    assert entry_rows(app_conn, tx) == [
        (bob.id, -300, "USD"),
        (alice.id, 100, "USD"),
        (carol.id, 200, "USD"),
    ]


def test_insert_files_the_rows_under_the_given_transaction_only(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    first = claim(api, app_conn)
    second = claim(api, app_conn)

    api._insert_entries(
        app_conn, second, [api.Entry(alice.id, -5, "USD"), api.Entry(bob.id, 5, "USD")]
    )

    assert entry_rows(app_conn, first) == []
    assert len(entry_rows(app_conn, second)) == 2


def test_insert_does_not_touch_the_balances(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """Updating balances is _apply_balance_deltas's job; the two steps stay separate."""
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    tx = claim(api, app_conn)
    before = balances(app_conn)

    api._insert_entries(app_conn, tx, [api.Entry(alice.id, -5, "USD"), api.Entry(bob.id, 5, "USD")])

    assert count(app_conn, "entries") == 2  # the insert happened, so "unchanged" means something
    assert balances(app_conn) == before


def test_insert_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    api,
) -> None:
    """The accounts and the claim are uncommitted too, so if the helper committed, everything
    would become visible to the owner connection below."""
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    tx = claim(api, app_conn)

    api._insert_entries(app_conn, tx, [api.Entry(alice.id, -5, "USD"), api.Entry(bob.id, 5, "USD")])

    assert count(app_conn, "entries") == 2
    app_conn.rollback()
    assert count(owner_conn, "entries") == 0
    assert count(owner_conn, "transactions") == 0


# --- _apply_balance_deltas --------------------------------------------------------------------


def test_apply_adds_each_delta_to_its_accounts_balance(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    funding = make(app_conn, "funding:usd", allow_negative=True)
    set_balance(app_conn, alice, 1000)
    set_balance(app_conn, bob, 50)
    set_balance(app_conn, funding, -1050)

    result = api._apply_balance_deltas(app_conn, {alice.id: -400, bob.id: 150, funding.id: 250})

    assert result is None
    assert balance(app_conn, alice) == 600
    assert balance(app_conn, bob) == 200
    assert balance(app_conn, funding) == -800


def test_apply_changes_only_the_accounts_in_the_map(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    carol = make(app_conn, "customer:carol")
    for account, amount in ((alice, 100), (bob, 200), (carol, 300)):
        set_balance(app_conn, account, amount)

    api._apply_balance_deltas(app_conn, {alice.id: 5})

    assert balances(app_conn) == {alice.id: 105, bob.id: 200, carol.id: 300}


def test_apply_with_a_zero_delta_leaves_the_balance_alone(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    set_balance(app_conn, alice, 70)
    api._apply_balance_deltas(app_conn, {alice.id: 0})
    assert balance(app_conn, alice) == 70


def test_apply_with_no_deltas_changes_nothing(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    set_balance(app_conn, alice, 70)
    api._apply_balance_deltas(app_conn, {})
    assert balances(app_conn) == {alice.id: 70}


def test_apply_does_not_check_for_an_overdraft_the_database_constraint_is_the_safety_net(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """The overdraft check belongs to post_transaction. If it ever let a protected account go
    negative through here, the balances CHECK refuses, and that is a bug (a logged 500), not a
    LedgerError."""
    alice = make(app_conn, "customer:alice")
    set_balance(app_conn, alice, 10)

    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        api._apply_balance_deltas(app_conn, {alice.id: -11})

    assert not isinstance(failure.value, LedgerError)


def test_apply_lets_an_allow_negative_account_go_below_zero(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    api._apply_balance_deltas(app_conn, {funding.id: -500})
    assert balance(app_conn, funding) == -500


def test_apply_to_an_account_with_no_balances_row_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """_lock_accounts has already established that every row exists, so a delta for a missing one
    means the caller is wrong. An UPDATE that matched nothing must not pass silently."""
    alice = make(app_conn, "customer:alice")
    set_balance(app_conn, alice, 10)

    with pytest.raises(LedgerInvariantError) as failure:
        api._apply_balance_deltas(app_conn, {alice.id: 5, 999_999: 5})

    assert not isinstance(failure.value, LedgerError)


def test_apply_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    commit_setup,
    api,
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "funding:usd", allow_negative=True)
    commit_setup()

    api._apply_balance_deltas(app_conn, {alice.id: 40, bob.id: -40})

    assert (balance(app_conn, alice), balance(app_conn, bob)) == (40, -40)
    app_conn.rollback()
    assert (balance(owner_conn, alice), balance(owner_conn, bob)) == (0, 0)
