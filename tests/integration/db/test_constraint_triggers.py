"""The commit-time safety nets (docs/design.md, milestone 2): deferred constraint triggers.

post_transaction already refuses entries that do not sum to zero per currency, and the claim and
posting always happen together. These triggers are the second line of defense, so a bug elsewhere
cannot commit a broken ledger:

- entries: when a transaction commits, its entries must sum to zero within each currency.
- transactions: a transaction cannot commit without at least one entry.

Both are deferred constraint triggers, so they fire when the database transaction commits, not at
each statement. A transaction can insert its row first and its entries after, which is exactly what
claim_transaction followed by post_transaction does.

A violation is a bug, not a normal outcome: it is a database error, never a LedgerError, and the
API layer turns it into a logged 500 (milestone 3). The tests accept either SQLSTATE a trigger would
naturally use, check_violation or raise_exception; the API's mapping decides which one it needs.

These tests commit transactions and entries on purpose, because the commit is what they test. The
commit_setup fixture truncates everything afterwards.
"""

import psycopg
import pytest

from ledger.accounts import Account
from ledger.errors import LedgerError
from tests.db_helpers import VALID_HASH, make_account

SAFETY_NET = (psycopg.errors.CheckViolation, psycopg.errors.RaiseException)


@pytest.fixture
def api():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger import posting

    return posting


def insert_transaction(conn: psycopg.Connection, key: str) -> int:
    row = conn.execute(
        "INSERT INTO transactions (idempotency_key, request_hash) VALUES (%s, %s) RETURNING id",
        (key, VALID_HASH),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_entry(
    conn: psycopg.Connection, transaction_id: int, account: Account, amount: int
) -> None:
    conn.execute(
        "INSERT INTO entries (transaction_id, account_id, amount_minor, currency)"
        " VALUES (%s, %s, %s, %s)",
        (transaction_id, account.id, amount, account.currency),
    )


def committed(owner_conn: psycopg.Connection) -> tuple[int, int]:
    """(entries, transactions) as another connection sees them, so only committed rows count."""
    entries = owner_conn.execute("SELECT count(*) FROM entries").fetchone()
    transactions = owner_conn.execute("SELECT count(*) FROM transactions").fetchone()
    assert entries is not None and transactions is not None
    return entries[0], transactions[0]


@pytest.fixture
def accounts(clean_db: None, app_conn: psycopg.Connection, commit_setup) -> dict[str, Account]:
    """Committed accounts: two in USD, two in EUR, and a USD funding account."""
    made = {
        "alice": make_account(app_conn, "customer:alice", "USD"),
        "bob": make_account(app_conn, "customer:bob", "USD"),
        "eve": make_account(app_conn, "customer:eve", "EUR"),
        "finn": make_account(app_conn, "customer:finn", "EUR"),
        "funding": make_account(app_conn, "funding:usd", "USD", allow_negative=True),
    }
    commit_setup()
    return made


# --- the triggers exist, and they are deferred constraint triggers ----------------------------


@pytest.mark.parametrize("table", ["entries", "transactions"])
def test_the_table_has_a_deferred_constraint_trigger(
    owner_conn: psycopg.Connection, table: str
) -> None:
    """An ordinary trigger would fire at each statement, before a transaction's second entry
    exists. Internal triggers (the foreign keys' own) are not ours."""
    row = owner_conn.execute(
        "SELECT count(*) FROM pg_trigger"
        " WHERE tgrelid = %s::regclass AND NOT tgisinternal AND tgconstraint <> 0"
        " AND tgdeferrable AND tginitdeferred",
        (f"public.{table}",),
    ).fetchone()
    assert row is not None
    assert row[0] >= 1


# --- entries: each transaction sums to zero within each currency, checked at commit -----------


def test_a_balanced_transaction_commits(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    tx = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, tx, accounts["alice"], -100)
    insert_entry(app_conn, tx, accounts["bob"], 100)
    app_conn.commit()
    assert committed(owner_conn) == (2, 1)


def test_an_unbalanced_transaction_fails_at_commit_and_nothing_is_kept(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    tx = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, tx, accounts["alice"], -100)
    insert_entry(app_conn, tx, accounts["bob"], 90)
    with pytest.raises(SAFETY_NET) as failure:
        app_conn.commit()
    assert not isinstance(failure.value, LedgerError)
    assert committed(owner_conn) == (0, 0)


def test_the_check_waits_for_commit(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    """After the first entry alone the transaction is unbalanced, and nothing complains yet."""
    tx = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, tx, accounts["alice"], -100)  # no error here
    insert_entry(app_conn, tx, accounts["bob"], 100)  # nor here
    app_conn.commit()
    assert committed(owner_conn) == (2, 1)


def test_each_currency_must_balance_not_just_the_total(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    """-100 USD and +100 EUR sum to zero across currencies, but not within either."""
    tx = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, tx, accounts["alice"], -100)
    insert_entry(app_conn, tx, accounts["eve"], 100)
    with pytest.raises(SAFETY_NET):
        app_conn.commit()
    assert committed(owner_conn) == (0, 0)


def test_a_transaction_in_two_currencies_commits_when_each_balances(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    tx = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, tx, accounts["alice"], -700)
    insert_entry(app_conn, tx, accounts["bob"], 700)
    insert_entry(app_conn, tx, accounts["eve"], -900)
    insert_entry(app_conn, tx, accounts["finn"], 900)
    app_conn.commit()
    assert committed(owner_conn) == (4, 1)


def test_each_transaction_is_checked_on_its_own(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    """The first is 50 over and the second 50 under, so all the entries together sum to zero.
    Neither transaction balances by itself."""
    first = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, first, accounts["alice"], 100)
    insert_entry(app_conn, first, accounts["bob"], -50)
    second = insert_transaction(app_conn, "key-2")
    insert_entry(app_conn, second, accounts["alice"], -100)
    insert_entry(app_conn, second, accounts["bob"], 50)
    with pytest.raises(SAFETY_NET):
        app_conn.commit()
    assert committed(owner_conn) == (0, 0)


def test_one_bad_transaction_stops_the_whole_commit(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    good = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, good, accounts["alice"], -100)
    insert_entry(app_conn, good, accounts["bob"], 100)
    bad = insert_transaction(app_conn, "key-2")
    insert_entry(app_conn, bad, accounts["alice"], -100)
    insert_entry(app_conn, bad, accounts["bob"], 99)
    with pytest.raises(SAFETY_NET):
        app_conn.commit()
    assert committed(owner_conn) == (0, 0)


# --- transactions: at least one entry, checked at commit --------------------------------------


def test_a_transaction_with_no_entries_fails_at_commit(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    insert_transaction(app_conn, "key-1")
    with pytest.raises(SAFETY_NET) as failure:
        app_conn.commit()
    assert not isinstance(failure.value, LedgerError)
    assert committed(owner_conn) == (0, 0)


def test_a_claim_that_was_never_posted_cannot_be_committed(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts, api
) -> None:
    """The bug this guards against: a caller claims the key, skips post_transaction, and commits."""
    api.claim_transaction(app_conn, "key-1", VALID_HASH)
    with pytest.raises(SAFETY_NET):
        app_conn.commit()
    assert committed(owner_conn) == (0, 0)


def test_one_transaction_without_entries_stops_a_commit_that_has_a_good_one(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts
) -> None:
    good = insert_transaction(app_conn, "key-1")
    insert_entry(app_conn, good, accounts["alice"], -100)
    insert_entry(app_conn, good, accounts["bob"], 100)
    insert_transaction(app_conn, "key-2")
    with pytest.raises(SAFETY_NET):
        app_conn.commit()
    assert committed(owner_conn) == (0, 0)


# --- the real path still commits --------------------------------------------------------------


def test_a_claimed_and_posted_transfer_commits(
    app_conn: psycopg.Connection, owner_conn: psycopg.Connection, accounts, api
) -> None:
    """The claim inserts the transactions row before any entry exists. Because the triggers are
    deferred, claim_transaction followed by post_transaction commits as one unit."""
    tx = api.claim_transaction(app_conn, "key-1", VALID_HASH).transaction_id
    api.post_transaction(
        app_conn,
        tx,
        [
            api.Entry(accounts["funding"].id, -500, "USD"),
            api.Entry(accounts["alice"].id, 500, "USD"),
        ],
    )
    app_conn.commit()

    assert committed(owner_conn) == (2, 1)
    row = owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (accounts["alice"].id,)
    ).fetchone()
    assert row == (500,)
