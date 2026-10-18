"""The entry history SQL (ledger/repository/account_repo.py; ADR 0013; docs/design.md, GET
/accounts/{id}/entries; issue #82).

    account_exists(conn, account_id) -> bool
    find_entries(conn, account_id, before_id, limit) -> list[HistoryEntry]

`HistoryEntry(id, transaction_id, account_id, amount_minor, currency, created_at)` is in
ledger/models/entry_history_model.py: the transfer entry shape plus the owning transaction's id and
`created_at` (a timezone-aware datetime read by joining `transactions`).

Plain functions that run one read statement each on the connection they are given and return what
the database says. They never judge it: an account that does not exist is `False`, and an account
with no entries is an empty list; the service decides that the first is a 404. `find_entries`:

- returns only the entries of the given account, newest first by entry id, never by `created_at`
  (a transfer that started earlier can commit later, so the two can disagree);
- returns entries with an id strictly below `before_id`, or from the newest when `before_id` is
  None;
- returns at most `limit` rows, and does exactly what it is asked: the `limit + 1` trick is the
  service's job, not the repository's.

They run only reads, take no row locks and do not commit.
"""

import itertools
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ledger.models.posting_model import Entry
from ledger.service.posting_service import claim_transaction, post_transaction
from tests.db_helpers import hash_for, make_account

BIGINT_MAX = 9_223_372_036_854_775_807

# Every transfer needs its own idempotency key, even across calls of fund().
_keys = itertools.count(1)


@pytest.fixture
def history_entry():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.models.entry_history_model import HistoryEntry

    return HistoryEntry


@pytest.fixture
def repo():
    """Imported lazily so these tests fail one by one while the functions do not exist yet."""
    from ledger.repository import account_repo

    return account_repo


def post(conn: psycopg.Connection, key: str, entries: list[Entry]) -> int:
    claim = claim_transaction(conn, key, hash_for(key))
    post_transaction(conn, claim.transaction_id, entries)
    return claim.transaction_id


def fund(conn: psycopg.Connection, funding_id: int, account_id: int, count: int) -> list[int]:
    """Post `count` separate transfers of 10 from the funding account to the account. Returns the
    account's entry ids, oldest first."""
    for _ in range(count):
        post(
            conn,
            f"fund:{next(_keys)}",
            [Entry(funding_id, -10, "USD"), Entry(account_id, 10, "USD")],
        )
    rows = conn.execute(
        "SELECT id FROM entries WHERE account_id = %s ORDER BY id", (account_id,)
    ).fetchall()
    return [row[0] for row in rows]


def ids(entries) -> list[int]:
    return [entry.id for entry in entries]


# --- account_exists ---------------------------------------------------------------------------


def test_an_existing_account_exists(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    alice = make_account(app_conn, "customer:alice")

    assert repo.account_exists(app_conn, alice.id) is True


def test_an_account_with_no_entries_still_exists(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = make_account(app_conn, "customer:alice")

    assert repo.find_entries(app_conn, alice.id, None, 10) == []
    assert repo.account_exists(app_conn, alice.id) is True


def test_an_unknown_account_does_not_exist(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    make_account(app_conn, "customer:alice")

    assert repo.account_exists(app_conn, 999_999) is False


def test_an_id_at_the_bigint_maximum_is_just_missing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The API bounds ids before they get here, but the largest legal one must not be a database
    error."""
    assert repo.account_exists(app_conn, BIGINT_MAX) is False


# --- find_entries: what comes back ------------------------------------------------------------


def test_it_returns_the_entries_of_the_account_newest_first_by_entry_id(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 4)

    result = repo.find_entries(app_conn, alice.id, None, 10)

    assert ids(result) == sorted(entry_ids, reverse=True)


def test_each_entry_carries_the_stored_values_and_its_transactions_created_at(
    clean_db: None, app_conn: psycopg.Connection, repo, history_entry
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -25, "USD"), Entry(alice.id, 25, "USD")])
    stored = app_conn.execute(
        "SELECT e.id, e.transaction_id, e.account_id, e.amount_minor, e.currency, t.created_at"
        " FROM entries e JOIN transactions t ON t.id = e.transaction_id"
        " WHERE e.account_id = %s",
        (alice.id,),
    ).fetchone()

    result = repo.find_entries(app_conn, alice.id, None, 10)

    assert result == [history_entry(*stored)]
    assert result[0].transaction_id == tx
    assert result[0].account_id == alice.id
    assert result[0].amount_minor == 25
    assert result[0].currency == "USD"
    assert result[0].created_at.tzinfo is not None


def test_the_sign_of_the_amount_is_kept(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    post(app_conn, "k1", [Entry(funding.id, -25, "USD"), Entry(alice.id, 25, "USD")])

    assert [e.amount_minor for e in repo.find_entries(app_conn, funding.id, None, 10)] == [-25]
    assert [e.amount_minor for e in repo.find_entries(app_conn, alice.id, None, 10)] == [25]


def test_only_the_asked_accounts_entries_come_back(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    alice_ids = fund(app_conn, funding.id, alice.id, 2)
    bob_ids = fund(app_conn, funding.id, bob.id, 3)

    assert sorted(ids(repo.find_entries(app_conn, alice.id, None, 10))) == alice_ids
    assert sorted(ids(repo.find_entries(app_conn, bob.id, None, 10))) == bob_ids
    assert len(repo.find_entries(app_conn, funding.id, None, 10)) == 5


def test_an_account_with_no_entries_gives_an_empty_list(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    fund(app_conn, funding.id, bob.id, 2)

    assert repo.find_entries(app_conn, alice.id, None, 10) == []


def test_an_unknown_account_gives_an_empty_list_and_not_an_error(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """Deciding that this is a 404 is the service's job."""
    assert repo.find_entries(app_conn, 999_999, None, 10) == []


# --- find_entries: the order is by entry id, not by time -------------------------------------


def test_the_order_follows_entry_id_even_when_created_at_disagrees(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """A transfer that started earlier can wait for a lock and commit later, so a transaction with
    a later entry id can carry an earlier created_at. The first transaction here gets the later
    timestamp, so ordering by created_at would put its entry first."""
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    now = datetime.now(UTC)
    first = insert_transaction(app_conn, "k1", now + timedelta(hours=1))
    second = insert_transaction(app_conn, "k2", now - timedelta(hours=1))
    post_transaction(app_conn, first, [Entry(funding.id, -1, "USD"), Entry(alice.id, 1, "USD")])
    post_transaction(app_conn, second, [Entry(funding.id, -2, "USD"), Entry(alice.id, 2, "USD")])

    result = repo.find_entries(app_conn, alice.id, None, 10)

    assert [e.transaction_id for e in result] == [second, first]
    assert result[0].created_at < result[1].created_at
    assert ids(result) == sorted(ids(result), reverse=True)


def insert_transaction(conn: psycopg.Connection, key: str, created_at: datetime) -> int:
    row = conn.execute(
        "INSERT INTO transactions (idempotency_key, request_hash, created_at)"
        " VALUES (%s, %s, %s) RETURNING id",
        (key, hash_for(key), created_at),
    ).fetchone()
    assert row is not None
    return row[0]


# --- find_entries: before_id ------------------------------------------------------------------


def test_before_id_is_exclusive_and_returns_only_older_entries(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 5)
    third = entry_ids[2]

    result = repo.find_entries(app_conn, alice.id, third, 10)

    assert ids(result) == [entry_ids[1], entry_ids[0]]


def test_before_id_works_when_the_ids_of_one_account_are_not_consecutive(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """Other accounts' entries sit between Alice's ids, so the cursor must compare ids and never
    count rows."""
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    for _ in range(3):
        fund(app_conn, funding.id, alice.id, 1)
        fund(app_conn, funding.id, bob.id, 2)
    alice_ids = sorted(ids(repo.find_entries(app_conn, alice.id, None, 10)))
    assert alice_ids[1] - alice_ids[0] > 1

    result = repo.find_entries(app_conn, alice.id, alice_ids[2], 10)

    assert ids(result) == [alice_ids[1], alice_ids[0]]


def test_before_the_oldest_entry_gives_an_empty_list(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    assert repo.find_entries(app_conn, alice.id, entry_ids[0], 10) == []


def test_before_an_id_above_the_newest_returns_everything(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    result = repo.find_entries(app_conn, alice.id, BIGINT_MAX, 10)

    assert ids(result) == sorted(entry_ids, reverse=True)


def test_before_id_none_starts_from_the_newest_entry(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    assert repo.find_entries(app_conn, alice.id, None, 1)[0].id == entry_ids[-1]


# --- find_entries: limit ----------------------------------------------------------------------


def test_limit_caps_the_rows_and_keeps_the_newest(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 5)

    result = repo.find_entries(app_conn, alice.id, None, 2)

    assert ids(result) == [entry_ids[4], entry_ids[3]]


def test_a_limit_above_the_number_of_entries_returns_them_all(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    assert len(repo.find_entries(app_conn, alice.id, None, 201)) == len(entry_ids)


def test_a_limit_equal_to_the_number_of_entries_returns_them_all(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    assert ids(repo.find_entries(app_conn, alice.id, None, 3)) == sorted(entry_ids, reverse=True)


def test_limit_and_before_id_combine(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    entry_ids = fund(app_conn, funding.id, alice.id, 6)

    result = repo.find_entries(app_conn, alice.id, entry_ids[4], 2)

    assert ids(result) == [entry_ids[3], entry_ids[2]]


# --- it only reads ----------------------------------------------------------------------------


def test_it_runs_in_a_read_only_transaction_and_changes_nothing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    fund(app_conn, funding.id, alice.id, 3)
    app_conn.execute("SET TRANSACTION READ ONLY")
    tables = ("transactions", "entries", "balances", "accounts")
    before = [app_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables]

    repo.account_exists(app_conn, alice.id)
    repo.find_entries(app_conn, alice.id, None, 2)

    after = [app_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables]
    assert after == before
