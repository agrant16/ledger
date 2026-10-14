"""insert_entries and apply_balance_deltas (ledger/repository/posting_repo.py; ADR 0013): the two
write steps.

post_transaction (the service) checks the entries and locks the balances rows first. These two
repository functions then do the writing, and trust what they are given:

    insert_entries(conn, transaction_id, entries) -> None
        one entries row per Entry, in list order, against the given transaction.
    apply_balance_deltas(conn, deltas) -> set[int]
        adds each delta to that account's cached balance and returns the ids of the accounts whose
        balances row it updated. It does not judge the result: an id that is missing from the set
        had no balances row, and deciding that this is a bug is the service's job.

Neither checks for an overdraft: that is the service's job, using the locked balances, and the
balances CHECK constraint is only the safety net behind it. Any integrity error is translated into
a LedgerInvariantError chained to the psycopg error (ADR 0013). Neither commits; the caller owns
the transaction.
"""

import itertools

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError
from ledger.models.account_model import Account
from ledger.models.posting_model import Entry
from tests.db_helpers import VALID_HASH, make_account

KEYS = itertools.count(1)


@pytest.fixture
def repo():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.repository import posting_repo

    return posting_repo


def claim(repo, conn: psycopg.Connection) -> int:
    transaction_id = repo.insert_claim(conn, f"key-{next(KEYS)}", VALID_HASH, None)
    assert transaction_id is not None
    return transaction_id


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


# --- insert_entries --------------------------------------------------------------------------


def test_insert_writes_one_row_per_entry_in_list_order(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    carol = make_account(app_conn, "customer:carol")
    tx = claim(repo, app_conn)
    entries = [
        Entry(bob.id, -300, "USD"),
        Entry(alice.id, 100, "USD"),
        Entry(carol.id, 200, "USD"),
    ]

    result = repo.insert_entries(app_conn, tx, entries)

    assert result is None
    assert entry_rows(app_conn, tx) == [
        (bob.id, -300, "USD"),
        (alice.id, 100, "USD"),
        (carol.id, 200, "USD"),
    ]


def test_insert_files_the_rows_under_the_given_transaction_only(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    first = claim(repo, app_conn)
    second = claim(repo, app_conn)

    repo.insert_entries(app_conn, second, [Entry(alice.id, -5, "USD"), Entry(bob.id, 5, "USD")])

    assert entry_rows(app_conn, first) == []
    assert len(entry_rows(app_conn, second)) == 2


def test_insert_does_not_touch_the_balances(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """Updating balances is apply_balance_deltas's job; the two steps stay separate."""
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    tx = claim(repo, app_conn)
    before = balances(app_conn)

    repo.insert_entries(app_conn, tx, [Entry(alice.id, -5, "USD"), Entry(bob.id, 5, "USD")])

    assert count(app_conn, "entries") == 2  # the insert happened, so "unchanged" means something
    assert balances(app_conn) == before


def test_insert_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    repo,
) -> None:
    """The accounts and the claim are uncommitted too, so if the helper committed, everything
    would become visible to the owner connection below."""
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    tx = claim(repo, app_conn)

    repo.insert_entries(app_conn, tx, [Entry(alice.id, -5, "USD"), Entry(bob.id, 5, "USD")])

    assert count(app_conn, "entries") == 2
    app_conn.rollback()
    assert count(owner_conn, "entries") == 0
    assert count(owner_conn, "transactions") == 0


# --- apply_balance_deltas --------------------------------------------------------------------


def test_apply_adds_each_delta_to_its_accounts_balance(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    set_balance(app_conn, alice, 1000)
    set_balance(app_conn, bob, 50)
    set_balance(app_conn, funding, -1050)

    result = repo.apply_balance_deltas(app_conn, {alice.id: -400, bob.id: 150, funding.id: 250})

    assert result == {alice.id, bob.id, funding.id}
    assert balance(app_conn, alice) == 600
    assert balance(app_conn, bob) == 200
    assert balance(app_conn, funding) == -800


def test_apply_changes_only_the_accounts_in_the_map(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    carol = make_account(app_conn, "customer:carol")
    for account, amount in ((alice, 100), (bob, 200), (carol, 300)):
        set_balance(app_conn, account, amount)

    result = repo.apply_balance_deltas(app_conn, {alice.id: 5})

    assert result == {alice.id}
    assert balances(app_conn) == {alice.id: 105, bob.id: 200, carol.id: 300}


def test_apply_with_a_zero_delta_leaves_the_balance_alone(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    set_balance(app_conn, alice, 70)
    assert repo.apply_balance_deltas(app_conn, {alice.id: 0}) == {alice.id}
    assert balance(app_conn, alice) == 70


def test_apply_with_no_deltas_changes_nothing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")
    set_balance(app_conn, alice, 70)
    assert repo.apply_balance_deltas(app_conn, {}) == set()
    assert balances(app_conn) == {alice.id: 70}


def test_apply_does_not_check_for_an_overdraft_the_database_constraint_is_the_safety_net(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The overdraft check belongs to post_transaction. If it ever let a protected account go
    negative through here, the balances CHECK refuses, and that is a bug (a logged 500), not a
    LedgerError."""
    alice = make_account(app_conn, "customer:alice")
    set_balance(app_conn, alice, 10)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.apply_balance_deltas(app_conn, {alice.id: -11})

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)


def test_apply_lets_an_allow_negative_account_go_below_zero(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    repo.apply_balance_deltas(app_conn, {funding.id: -500})
    assert balance(app_conn, funding) == -500


def test_apply_reports_which_accounts_it_updated_and_leaves_the_judging_to_the_service(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The lock step has already established that every row exists, so a delta for a missing one
    means the caller is wrong. Whether that is a bug is the service's call: this function only says
    which accounts had a balances row to update, so an UPDATE that matched nothing is visible."""
    alice = make_account(app_conn, "customer:alice")
    set_balance(app_conn, alice, 10)

    result = repo.apply_balance_deltas(app_conn, {alice.id: 5, 999_999: 5})

    assert result == {alice.id}
    assert balance(app_conn, alice) == 15


def test_apply_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    commit_setup,
    repo,
) -> None:
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "funding:usd", allow_negative=True)
    commit_setup()

    repo.apply_balance_deltas(app_conn, {alice.id: 40, bob.id: -40})

    assert (balance(app_conn, alice), balance(app_conn, bob)) == (40, -40)
    app_conn.rollback()
    assert (balance(owner_conn, alice), balance(owner_conn, bob)) == (0, 0)


# --- insert_entries: every integrity error is a bug, translated (ADR 0013) --------------------


def test_insert_with_no_entries_writes_nothing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    tx = claim(repo, app_conn)

    assert repo.insert_entries(app_conn, tx, []) is None

    assert count(app_conn, "entries") == 0


def test_an_entry_of_zero_is_a_bug(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    alice = make_account(app_conn, "customer:alice")
    tx = claim(repo, app_conn)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_entries(app_conn, tx, [Entry(alice.id, 0, "USD")])

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)


def test_an_entry_for_an_unknown_account_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    tx = claim(repo, app_conn)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_entries(app_conn, tx, [Entry(999_999, 5, "USD")])

    assert isinstance(failure.value.__cause__, psycopg.errors.ForeignKeyViolation)


def test_an_entry_in_the_wrong_currency_for_its_account_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The foreign key is on (account_id, currency), so the database refuses a mismatch."""
    alice = make_account(app_conn, "customer:alice", "USD")
    tx = claim(repo, app_conn)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_entries(app_conn, tx, [Entry(alice.id, 5, "EUR")])

    assert isinstance(failure.value.__cause__, psycopg.errors.ForeignKeyViolation)


def test_an_entry_for_an_unknown_transaction_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_entries(app_conn, 999_999, [Entry(alice.id, 5, "USD")])

    assert isinstance(failure.value.__cause__, psycopg.errors.ForeignKeyViolation)
