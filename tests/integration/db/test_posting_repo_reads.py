"""find_transfer (ledger/repository/posting_repo.py; ADR 0013): the SQL that reads a transfer.

    find_transfer(conn, transfer_id) -> Transfer, or None

Plain function that reads the `transactions` row and its `entries` and returns what the database
says, as the models in ledger/models/posting_model.py: `Transfer(id, reverses_transaction_id,
created_at, entries)` with `entries` a tuple of `TransferEntry(id, account_id, amount_minor,
currency)` in entry-id order, and `created_at` a timezone-aware UTC datetime. It never judges:
no such transaction is `None`, and a transaction with no entries is a Transfer whose `entries` is
empty; the service decides that the first is a 404 and the second a bug. It runs only reads (it
works in a read-only transaction), takes no row locks, and does not commit.
"""

from datetime import timedelta

import psycopg
import pytest

from ledger.models.posting_model import Entry
from ledger.service.posting_service import claim_transaction, post_transaction
from tests.db_helpers import hash_for, make_account


@pytest.fixture
def repo():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.repository import posting_repo

    return posting_repo


@pytest.fixture
def models():
    from ledger.models import posting_model

    return posting_model


def post(conn: psycopg.Connection, key: str, entries: list[Entry], reverses=None) -> int:
    claim = claim_transaction(conn, key, hash_for(key), reverses)
    post_transaction(conn, claim.transaction_id, entries)
    return claim.transaction_id


def test_it_returns_the_transaction_and_its_entries_in_entry_id_order(
    clean_db: None, app_conn: psycopg.Connection, repo, models
) -> None:
    low = make_account(app_conn, "funding:usd", allow_negative=True)
    high = make_account(app_conn, "customer:high")
    assert low.id < high.id
    tx = post(app_conn, "k1", [Entry(high.id, 100, "USD"), Entry(low.id, -100, "USD")])
    stored = app_conn.execute(
        "SELECT id, account_id, amount_minor, currency FROM entries"
        " WHERE transaction_id = %s ORDER BY id",
        (tx,),
    ).fetchall()

    result = repo.find_transfer(app_conn, tx)

    assert isinstance(result, models.Transfer)
    assert result.id == tx
    assert result.reverses_transaction_id is None
    assert result.entries == tuple(models.TransferEntry(*row) for row in stored)
    assert [entry.account_id for entry in result.entries] == [high.id, low.id]


def test_a_missing_transaction_is_none(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    assert repo.find_transfer(app_conn, 999_999) is None


def test_a_transaction_with_no_entries_has_an_empty_entries_tuple(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    claim = claim_transaction(app_conn, "k1", hash_for("k1"))

    result = repo.find_transfer(app_conn, claim.transaction_id)

    assert result is not None
    assert result.entries == ()


def test_only_the_asked_transfers_entries_come_back(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    first = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    second = post(app_conn, "k2", [Entry(funding.id, -9, "USD"), Entry(alice.id, 9, "USD")])

    assert [e.amount_minor for e in repo.find_transfer(app_conn, first).entries] == [-5, 5]
    assert [e.amount_minor for e in repo.find_transfer(app_conn, second).entries] == [-9, 9]


def test_a_reversal_carries_the_id_it_reverses(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    original = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    reversal = post(
        app_conn,
        "k2",
        [Entry(funding.id, 5, "USD"), Entry(alice.id, -5, "USD")],
        reverses=original,
    )

    assert repo.find_transfer(app_conn, reversal).reverses_transaction_id == original


def test_created_at_is_aware_and_utc_whatever_the_session_time_zone(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    app_conn.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
    stored = app_conn.execute("SELECT created_at FROM transactions WHERE id = %s", (tx,)).fetchone()
    assert stored is not None

    created_at = repo.find_transfer(app_conn, tx).created_at

    assert created_at.utcoffset() == timedelta(0)
    assert created_at == stored[0]


def test_it_works_in_a_read_only_transaction_and_changes_nothing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    app_conn.execute("SET TRANSACTION READ ONLY")
    balances = app_conn.execute("SELECT * FROM balances ORDER BY account_id").fetchall()

    repo.find_transfer(app_conn, tx)

    assert app_conn.execute("SELECT * FROM balances ORDER BY account_id").fetchall() == balances
