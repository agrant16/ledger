"""post_transaction (docs/design.md, milestone 2): the core of the ledger.

Given a claimed transaction and its entries it checks them, locks the touched balances rows in
ascending account id order, and writes the entries and the balance changes together. It takes the
request's connection and never commits; the caller owns the transaction.

The order of the checks is fixed: nothing touches the database until the entries are known to
balance, then unknown accounts, then currency mismatches, then insufficient funds. A rejected post
writes nothing.
"""

import itertools

import psycopg
import pytest

from ledger.accounts import Account, create_account
from ledger.config import Settings
from ledger.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    LedgerError,
    LedgerInvariantError,
    UnbalancedEntriesError,
    UnknownAccountError,
)

KEYS = itertools.count(1)


@pytest.fixture
def api():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger import posting

    return posting


def make(
    conn: psycopg.Connection, name: str, currency: str = "USD", allow_negative: bool = False
) -> Account:
    return create_account(conn, name, currency, allow_negative)


def post(api, conn: psycopg.Connection, entries) -> int:
    """Claim a fresh transaction and post the entries to it, as a request would."""
    tx = api.claim_transaction(conn, f"key-{next(KEYS)}", "hash")
    api.post_transaction(conn, tx, entries)
    return tx


def transfer(api, amount: int, sender: Account, receiver: Account):
    """The two entries of an ordinary transfer, in the sender's currency."""
    return [
        api.Entry(sender.id, -amount, sender.currency),
        api.Entry(receiver.id, amount, receiver.currency),
    ]


def balance(conn: psycopg.Connection, account: Account) -> int:
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account.id,)
    ).fetchone()
    assert row is not None
    return row[0]


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


def snapshot(conn: psycopg.Connection) -> dict[int, int]:
    return dict(conn.execute("SELECT account_id, balance_minor FROM balances").fetchall())


def assert_nothing_written(conn: psycopg.Connection, before: dict[int, int]) -> None:
    """Checked before any rollback, so it proves the post wrote nothing, not that it was undone."""
    assert count(conn, "entries") == 0
    assert snapshot(conn) == before


# --- what a successful post writes ----------------------------------------------------------


def test_writes_the_entries_in_order_against_the_transaction(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    tx = post(api, app_conn, transfer(api, 5000, funding, alice))
    assert entry_rows(app_conn, tx) == [(funding.id, -5000, "USD"), (alice.id, 5000, "USD")]


def test_updates_the_balances_and_lets_the_funding_account_go_negative(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    post(api, app_conn, transfer(api, 5000, funding, alice))
    assert balance(app_conn, alice) == 5000
    assert balance(app_conn, funding) == -5000


def test_cached_balances_equal_the_sum_of_entries_after_several_postings(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """Invariant: the cached balance always equals the sum of that account's entries."""
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    post(api, app_conn, transfer(api, 5000, funding, alice))
    post(api, app_conn, transfer(api, 1200, alice, bob))
    post(api, app_conn, transfer(api, 300, bob, alice))
    assert count(app_conn, "entries") == 6
    assert (balance(app_conn, funding), balance(app_conn, alice), balance(app_conn, bob)) == (
        -5000,
        4100,
        900,
    )
    mismatches = app_conn.execute(
        "SELECT b.account_id FROM balances b"
        " LEFT JOIN (SELECT account_id, sum(amount_minor) AS total FROM entries"
        "            GROUP BY account_id) s ON s.account_id = b.account_id"
        " WHERE b.balance_minor <> COALESCE(s.total, 0)"
    ).fetchall()
    assert mismatches == []


def test_one_transaction_may_span_currencies_when_each_currency_balances(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    usd_funding = make(app_conn, "funding:usd", "USD", allow_negative=True)
    eur_funding = make(app_conn, "funding:eur", "EUR", allow_negative=True)
    alice = make(app_conn, "customer:alice", "USD")
    bob = make(app_conn, "customer:bob", "EUR")
    entries = transfer(api, 700, usd_funding, alice) + transfer(api, 900, eur_funding, bob)
    tx = post(api, app_conn, entries)
    assert len(entry_rows(app_conn, tx)) == 4
    assert (balance(app_conn, alice), balance(app_conn, bob)) == (700, 900)


# --- entries to the same account are netted -------------------------------------------------


def test_the_overdraft_check_uses_the_net_change_not_each_entry(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """Each entry alone fits within alice's 100, but together they take 160."""
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    post(api, app_conn, transfer(api, 100, funding, alice))
    before = snapshot(app_conn)
    entries_before = count(app_conn, "entries")
    entries = transfer(api, 80, alice, bob) + transfer(api, 80, alice, bob)
    with pytest.raises(InsufficientFundsError) as failure:
        post(api, app_conn, entries)
    assert failure.value.account_id == alice.id
    assert failure.value.balance_minor == 100
    assert failure.value.amount_minor == -160
    assert count(app_conn, "entries") == entries_before
    assert snapshot(app_conn) == before


def test_an_account_whose_entries_net_to_zero_needs_no_balance(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    entries = transfer(api, 100, alice, bob) + transfer(api, 100, bob, alice)
    tx = post(api, app_conn, entries)
    assert len(entry_rows(app_conn, tx)) == 4
    assert (balance(app_conn, alice), balance(app_conn, bob)) == (0, 0)


# --- rejections: the right error, and nothing written ----------------------------------------


def test_unbalanced_entries_are_a_bug_and_write_nothing(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    before = snapshot(app_conn)
    entries = [api.Entry(funding.id, -100, "USD"), api.Entry(alice.id, 90, "USD")]
    with pytest.raises(UnbalancedEntriesError) as failure:
        post(api, app_conn, entries)
    assert isinstance(failure.value, LedgerInvariantError)
    assert not isinstance(failure.value, LedgerError)
    assert_nothing_written(app_conn, before)


def test_each_currency_must_balance_even_if_the_total_is_zero(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """+100 USD and -100 EUR sum to zero across currencies, but not within either."""
    usd_funding = make(app_conn, "funding:usd", "USD", allow_negative=True)
    bob = make(app_conn, "customer:bob", "EUR")
    before = snapshot(app_conn)
    entries = [api.Entry(bob.id, -100, "EUR"), api.Entry(usd_funding.id, 100, "USD")]
    with pytest.raises(UnbalancedEntriesError):
        post(api, app_conn, entries)
    assert_nothing_written(app_conn, before)


def test_no_entries_at_all_is_a_bug(clean_db: None, app_conn: psycopg.Connection, api) -> None:
    """A transaction must have entries; the deferred trigger will check it again at commit."""
    with pytest.raises(LedgerInvariantError):
        post(api, app_conn, [])
    assert count(app_conn, "entries") == 0


def test_an_unknown_account_is_rejected(clean_db: None, app_conn: psycopg.Connection, api) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    before = snapshot(app_conn)
    entries = [api.Entry(funding.id, -100, "USD"), api.Entry(999_999, 100, "USD")]
    with pytest.raises(UnknownAccountError):
        post(api, app_conn, entries)
    assert_nothing_written(app_conn, before)


def test_the_unknown_account_error_names_every_missing_account(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    entries = [
        api.Entry(funding.id, -300, "USD"),
        api.Entry(999_998, 100, "USD"),
        api.Entry(999_999, 200, "USD"),
    ]
    with pytest.raises(UnknownAccountError) as failure:
        post(api, app_conn, entries)
    assert "999998" in str(failure.value)
    assert "999999" in str(failure.value)


def test_an_entry_in_the_wrong_currency_for_its_account_is_rejected(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """Invariant: every entry's currency matches its account's. This is the service's own check;
    the composite foreign key on entries is only the safety net behind it."""
    eur_funding = make(app_conn, "funding:eur", "EUR", allow_negative=True)
    alice = make(app_conn, "customer:alice", "USD")
    before = snapshot(app_conn)
    entries = [api.Entry(eur_funding.id, -100, "EUR"), api.Entry(alice.id, 100, "EUR")]
    with pytest.raises(CurrencyMismatchError):
        post(api, app_conn, entries)
    assert_nothing_written(app_conn, before)


def test_insufficient_funds_reports_the_account_balance_and_amount(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    before = snapshot(app_conn)
    with pytest.raises(InsufficientFundsError) as failure:
        post(api, app_conn, transfer(api, 1, alice, bob))
    assert isinstance(failure.value, LedgerError)
    assert failure.value.account_id == alice.id
    assert failure.value.balance_minor == 0
    assert failure.value.amount_minor == -1
    assert_nothing_written(app_conn, before)


def test_when_several_accounts_overdraw_the_lowest_account_id_is_reported(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    """Which account the error names must not depend on the order of the entries."""
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    carol = make(app_conn, "customer:carol")
    assert alice.id < bob.id < carol.id
    entries = [
        api.Entry(bob.id, -50, "USD"),  # bob comes first in the entries, but alice has the lower id
        api.Entry(alice.id, -50, "USD"),
        api.Entry(carol.id, 100, "USD"),
    ]
    before = snapshot(app_conn)
    with pytest.raises(InsufficientFundsError) as failure:
        post(api, app_conn, entries)
    assert failure.value.account_id == alice.id
    assert_nothing_written(app_conn, before)


def test_spending_exactly_the_balance_is_allowed_but_one_more_is_not(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    post(api, app_conn, transfer(api, 100, funding, alice))
    post(api, app_conn, transfer(api, 100, alice, bob))
    assert balance(app_conn, alice) == 0
    before = snapshot(app_conn)
    entries_before = count(app_conn, "entries")
    with pytest.raises(InsufficientFundsError):
        post(api, app_conn, transfer(api, 1, alice, bob))
    assert count(app_conn, "entries") == entries_before
    assert snapshot(app_conn) == before


# --- the fixed order of the checks ------------------------------------------------------------


def test_unbalanced_entries_are_reported_before_an_unknown_account(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    entries = [api.Entry(funding.id, -100, "USD"), api.Entry(999_999, 90, "USD")]
    with pytest.raises(UnbalancedEntriesError):
        post(api, app_conn, entries)


def test_an_unknown_account_is_reported_before_a_currency_mismatch(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    eur_funding = make(app_conn, "funding:eur", "EUR", allow_negative=True)
    alice = make(app_conn, "customer:alice", "USD")  # wrong currency for the EUR entry below
    entries = [
        api.Entry(eur_funding.id, -100, "EUR"),
        api.Entry(alice.id, 50, "EUR"),
        api.Entry(999_999, 50, "EUR"),
    ]
    with pytest.raises(UnknownAccountError):
        post(api, app_conn, entries)


def test_a_currency_mismatch_is_reported_before_insufficient_funds(
    clean_db: None, app_conn: psycopg.Connection, api
) -> None:
    alice = make(app_conn, "customer:alice", "USD")  # balance 0, so this would also overdraw
    bob = make(app_conn, "customer:bob", "EUR")
    entries = [api.Entry(alice.id, -100, "EUR"), api.Entry(bob.id, 100, "EUR")]
    with pytest.raises(CurrencyMismatchError):
        post(api, app_conn, entries)


# --- atomicity and the caller's transaction ---------------------------------------------------


def test_a_failure_between_the_inserts_and_the_balance_update_leaves_nothing(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    commit_setup,
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure injection: the entries are written, then the balance update raises."""
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    commit_setup()

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr("ledger.posting._apply_balance_deltas", boom)

    tx = api.claim_transaction(app_conn, "key-injected", "hash")
    with pytest.raises(RuntimeError, match="injected failure"):
        api.post_transaction(app_conn, tx, transfer(api, 5000, funding, alice))
    # The inserts ran first: the failure came after them, not before.
    assert len(entry_rows(app_conn, tx)) == 2

    app_conn.rollback()
    assert count(owner_conn, "entries") == 0
    assert count(owner_conn, "transactions") == 0
    assert balance(owner_conn, alice) == 0
    assert balance(owner_conn, funding) == 0


def test_it_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    commit_setup,
    api,
) -> None:
    """The caller owns the transaction, so a rollback must undo the entries and the balances."""
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    commit_setup()
    post(api, app_conn, transfer(api, 5000, funding, alice))
    assert (
        count(app_conn, "entries") == 2
    )  # the work happened, so the rollback below means something
    assert balance(app_conn, alice) == 5000
    app_conn.rollback()
    assert count(owner_conn, "entries") == 0
    assert count(owner_conn, "transactions") == 0
    assert (balance(owner_conn, alice), balance(owner_conn, funding)) == (0, 0)


def test_it_refuses_an_autocommit_connection(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    commit_setup,
    settings: Settings,
    api,
) -> None:
    funding = make(app_conn, "funding:usd", allow_negative=True)
    alice = make(app_conn, "customer:alice")
    commit_setup()
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            api.post_transaction(conn, 1, transfer(api, 5000, funding, alice))
    assert count(owner_conn, "entries") == 0
    assert (balance(owner_conn, alice), balance(owner_conn, funding)) == (0, 0)


# --- locking ----------------------------------------------------------------------------------


def test_the_touched_balance_rows_are_locked_before_the_overdraft_check(
    clean_db: None,
    app_conn: psycopg.Connection,
    app_pool,
    commit_setup,
    api,
) -> None:
    """The overdraft check must read a balance that is already locked, so the check and the update
    cannot be separated by another writer. The post below fails the check without writing; the
    rows it touched must still be locked, and an untouched account's row must not be."""
    alice = make(app_conn, "customer:alice")
    bob = make(app_conn, "customer:bob")
    carol = make(app_conn, "customer:carol")
    commit_setup()

    tx = api.claim_transaction(app_conn, "key-locks", "hash")
    with pytest.raises(InsufficientFundsError):
        api.post_transaction(app_conn, tx, transfer(api, 1, alice, bob))

    lock = "SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE NOWAIT"
    with app_pool.connection() as other:
        for touched in (alice, bob):
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(lock, (touched.id,))
            other.rollback()
        assert other.execute(lock, (carol.id,)).fetchone() == (1,)
        other.rollback()


def test_the_cleanup_fixture_leaves_the_database_empty(owner_conn: psycopg.Connection) -> None:
    """Runs after the tests that commit their accounts and checks commit_setup truncated them.

    It deliberately does not clean up itself. If the order ever changes it can only pass
    vacuously, never fail wrongly."""
    assert count(owner_conn, "accounts") == 0
