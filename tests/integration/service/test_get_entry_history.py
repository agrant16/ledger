"""get_entry_history (ledger/service/account_service.py; docs/design.md, GET
/accounts/{id}/entries; issue #82).

    get_entry_history(conn, account_id, before_id, limit) -> EntryPage(entries, has_more)

`EntryPage` is in ledger/models/entry_history_model.py: `entries` is a tuple of `HistoryEntry`
(newest first by entry id) and `has_more` says whether an older entry exists beyond the page.

The service decides what the repository's answers mean (the SQL is tested in
tests/integration/db/test_entry_history_repo.py):

- it checks the account first: no such account is UnknownAccountError (a LedgerError, a 404), while
  a real account with no entries is an empty page with has_more False;
- it asks the repository for `limit + 1` rows to learn whether another page exists, then returns
  only `limit` of them, so has_more is True exactly when an older entry exists;
- `before_id` is a plain entry id (the service knows nothing about cursors): only entries with a
  smaller id are returned;
- a `limit` below 1 or above 200 is a bug, not a client error (the API guarantees 1 to 200), so it
  is a LedgerInvariantError and never a LedgerError.

It changes nothing, and the caller owns the transaction.
"""

import itertools

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError, UnknownAccountError
from ledger.infra.config import Settings
from ledger.models.entry_history_model import EntryPage, HistoryEntry
from ledger.models.posting_model import Entry
from ledger.service.account_service import create_account, get_entry_history
from ledger.service.posting_service import claim_transaction, post_transaction
from tests.db_helpers import hash_for

# Every transfer needs its own idempotency key, even across calls of fund().
_keys = itertools.count(1)


def fund(conn: psycopg.Connection, funding_id: int, account_id: int, count: int) -> list[int]:
    """Post `count` separate transfers of 10 from the funding account to the account. Returns the
    account's entry ids, oldest first."""
    for _ in range(count):
        key = f"fund:{next(_keys)}"
        claim = claim_transaction(conn, key, hash_for(key))
        post_transaction(
            conn,
            claim.transaction_id,
            [Entry(funding_id, -10, "USD"), Entry(account_id, 10, "USD")],
        )
    rows = conn.execute(
        "SELECT id FROM entries WHERE account_id = %s ORDER BY id", (account_id,)
    ).fetchall()
    return [row[0] for row in rows]


def ids(page) -> list[int]:
    return [entry.id for entry in page.entries]


@pytest.fixture
def accounts(clean_db: None, app_conn: psycopg.Connection):
    """A funding account and Alice, who has no entries yet."""
    funding = create_account(app_conn, "funding:usd", "USD", True)
    alice = create_account(app_conn, "customer:alice", "USD", False)
    return funding, alice


# --- the account ------------------------------------------------------------------------------


def test_an_unknown_account_is_an_unknown_account_error(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    with pytest.raises(UnknownAccountError):
        get_entry_history(app_conn, 999_999, None, 50)


def test_an_unknown_account_is_a_ledger_error(clean_db: None, app_conn: psycopg.Connection) -> None:
    with pytest.raises(LedgerError):
        get_entry_history(app_conn, 999_999, None, 50)


def test_an_unknown_account_is_unknown_even_when_other_accounts_have_entries(
    accounts, app_conn: psycopg.Connection
) -> None:
    funding, alice = accounts
    fund(app_conn, funding.id, alice.id, 2)

    with pytest.raises(UnknownAccountError):
        get_entry_history(app_conn, alice.id + 1000, None, 50)


def test_a_real_account_with_no_entries_is_an_empty_page(
    accounts, app_conn: psycopg.Connection
) -> None:
    _, alice = accounts

    page = get_entry_history(app_conn, alice.id, None, 50)

    assert isinstance(page, EntryPage)
    assert list(page.entries) == []
    assert page.has_more is False


def test_an_empty_page_past_the_oldest_entry_is_not_an_error(
    accounts, app_conn: psycopg.Connection
) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 2)

    page = get_entry_history(app_conn, alice.id, entry_ids[0], 50)

    assert list(page.entries) == []
    assert page.has_more is False


# --- the page ---------------------------------------------------------------------------------


def test_it_returns_history_entries_newest_first(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 3)

    page = get_entry_history(app_conn, alice.id, None, 50)

    assert all(isinstance(entry, HistoryEntry) for entry in page.entries)
    assert ids(page) == sorted(entry_ids, reverse=True)
    newest = page.entries[0]
    assert newest.account_id == alice.id
    assert newest.amount_minor == 10
    assert newest.currency == "USD"
    assert newest.created_at.tzinfo is not None


def test_it_returns_only_the_accounts_own_entries(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    bob = create_account(app_conn, "customer:bob", "USD", False)
    alice_ids = fund(app_conn, funding.id, alice.id, 2)
    fund(app_conn, funding.id, bob.id, 3)

    page = get_entry_history(app_conn, alice.id, None, 50)

    assert sorted(ids(page)) == alice_ids


# --- limit + 1 and has_more -------------------------------------------------------------------


def test_fewer_entries_than_the_limit_is_one_page(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    fund(app_conn, funding.id, alice.id, 2)

    page = get_entry_history(app_conn, alice.id, None, 5)

    assert len(page.entries) == 2
    assert page.has_more is False


def test_exactly_limit_entries_has_no_more(accounts, app_conn: psycopg.Connection) -> None:
    """The boundary of the limit + 1 fetch: a full page with nothing behind it."""
    funding, alice = accounts
    fund(app_conn, funding.id, alice.id, 3)

    page = get_entry_history(app_conn, alice.id, None, 3)

    assert len(page.entries) == 3
    assert page.has_more is False


def test_one_entry_more_than_the_limit_has_more_and_returns_only_limit(
    accounts, app_conn: psycopg.Connection
) -> None:
    """The extra row is only used to learn there is another page; it is never returned."""
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 4)

    page = get_entry_history(app_conn, alice.id, None, 3)

    assert ids(page) == [entry_ids[3], entry_ids[2], entry_ids[1]]
    assert page.has_more is True


def test_many_more_entries_than_the_limit_returns_the_newest_limit(
    accounts, app_conn: psycopg.Connection
) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 7)

    page = get_entry_history(app_conn, alice.id, None, 2)

    assert ids(page) == [entry_ids[6], entry_ids[5]]
    assert page.has_more is True


def test_a_limit_of_one_returns_the_newest_entry(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 2)

    page = get_entry_history(app_conn, alice.id, None, 1)

    assert ids(page) == [entry_ids[1]]
    assert page.has_more is True


# --- before_id --------------------------------------------------------------------------------


def test_before_id_continues_where_the_previous_page_ended(
    accounts, app_conn: psycopg.Connection
) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 5)
    first = get_entry_history(app_conn, alice.id, None, 2)

    second = get_entry_history(app_conn, alice.id, first.entries[-1].id, 2)

    assert ids(first) == [entry_ids[4], entry_ids[3]]
    assert ids(second) == [entry_ids[2], entry_ids[1]]
    assert second.has_more is True


def test_the_last_page_has_no_more(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    entry_ids = fund(app_conn, funding.id, alice.id, 5)

    page = get_entry_history(app_conn, alice.id, entry_ids[2], 2)

    assert ids(page) == [entry_ids[1], entry_ids[0]]
    assert page.has_more is False


def test_before_id_does_not_skip_entries_of_other_accounts_ids(
    accounts, app_conn: psycopg.Connection
) -> None:
    """Bob's entries sit between Alice's ids; the cursor compares ids and never counts rows."""
    funding, alice = accounts
    bob = create_account(app_conn, "customer:bob", "USD", False)
    alice_ids: list[int] = []
    for _ in range(4):
        alice_ids = fund(app_conn, funding.id, alice.id, 1)  # all of Alice's ids so far
        fund(app_conn, funding.id, bob.id, 2)

    first = get_entry_history(app_conn, alice.id, None, 2)
    second = get_entry_history(app_conn, alice.id, first.entries[-1].id, 2)

    assert ids(first) + ids(second) == sorted(alice_ids, reverse=True)
    assert first.has_more is True
    assert second.has_more is False


# --- a limit below 1 is a bug -----------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_a_limit_below_one_is_an_invariant_error(
    accounts, app_conn: psycopg.Connection, limit: int
) -> None:
    _, alice = accounts

    with pytest.raises(LedgerInvariantError) as failure:
        get_entry_history(app_conn, alice.id, None, limit)

    assert not isinstance(failure.value, LedgerError)


# --- a limit above 200 is a bug too -----------------------------------------------------------


@pytest.mark.parametrize("limit", [201, 1000])
def test_a_limit_above_two_hundred_is_an_invariant_error(
    accounts, app_conn: psycopg.Connection, limit: int
) -> None:
    _, alice = accounts

    with pytest.raises(LedgerInvariantError) as failure:
        get_entry_history(app_conn, alice.id, None, limit)

    assert not isinstance(failure.value, LedgerError)


def test_a_limit_of_exactly_two_hundred_is_allowed(accounts, app_conn: psycopg.Connection) -> None:
    """The bound is inclusive: 200 is the largest page the API accepts."""
    _, alice = accounts

    page = get_entry_history(app_conn, alice.id, None, 200)

    assert page == EntryPage(entries=(), has_more=False)


# --- the transaction --------------------------------------------------------------------------


def test_it_refuses_an_autocommit_connection(clean_db: None, settings: Settings) -> None:
    """Every service function that is given a connection assumes a transaction is in progress."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            get_entry_history(conn, 1, None, 50)


def test_it_changes_nothing(accounts, app_conn: psycopg.Connection) -> None:
    funding, alice = accounts
    fund(app_conn, funding.id, alice.id, 3)
    tables = ("transactions", "entries", "balances", "accounts")
    before = [app_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables]

    get_entry_history(app_conn, alice.id, None, 2)
    get_entry_history(app_conn, alice.id, None, 2)

    after = [app_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables]
    assert after == before
