"""get_transfer (ledger/service/transfer_service.py; docs/design.md, Transfer response and
GET /transfers/{id}; ADR 0013).

    get_transfer(conn, transfer_id) -> Transfer
    Transfer(id, reverses_transaction_id, created_at, entries)
    TransferEntry(id, account_id, amount_minor, currency)   (in ledger/models/transfer_model.py)

The service reads the transfer through the repository (`posting_repo.find_transfer`) and decides
what the answer means; the SQL itself is tested in tests/integration/db/test_posting_repo_reads.py.
A transfer is a `transactions` row with its `entries`, and the answer is rebuilt from those stored
rows alone, which is why a replay of POST /transfers can return exactly what the original request
returned. `entries` is a tuple, in the order the entries were written (by entry id). `created_at`
is a timezone-aware datetime in UTC, whatever time zone the session uses. An unknown id raises
UnknownTransferError. A transaction with no entries cannot exist once it commits (the no-entries
trigger), so finding one is a bug and raises LedgerInvariantError, never an empty transfer.

It takes the request's connection, changes nothing, and the caller owns the transaction.
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError, UnknownTransferError
from ledger.infra.config import Settings
from ledger.models import transfer_model
from ledger.models.posting_model import Entry
from ledger.service.posting_service import claim_transaction, post_transaction
from tests.db_helpers import hash_for, make_account


@pytest.fixture
def get_transfer():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.service.transfer_service import get_transfer

    return get_transfer


@pytest.fixture
def types():
    from ledger.models import transfer_model

    return transfer_model


def post(
    conn: psycopg.Connection, key: str, entries: list[Entry], reverses: int | None = None
) -> int:
    claim = claim_transaction(conn, key, hash_for(key), reverses)
    post_transaction(conn, claim.transaction_id, entries)
    return claim.transaction_id


def entry_ids(conn: psycopg.Connection, transaction_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM entries WHERE transaction_id = %s ORDER BY id", (transaction_id,)
    ).fetchall()
    return [row[0] for row in rows]


def test_it_returns_the_transfer_with_its_entries(
    clean_db: None, app_conn: psycopg.Connection, get_transfer, types
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -500, "USD"), Entry(alice.id, 500, "USD")])
    first, second = entry_ids(app_conn, tx)

    result = get_transfer(app_conn, tx)

    assert isinstance(result, types.Transfer)
    assert result.id == tx
    assert result.reverses_transaction_id is None
    assert result.entries == (
        transfer_model.TransferEntry(first, funding.id, -500, "USD"),
        transfer_model.TransferEntry(second, alice.id, 500, "USD"),
    )


def test_the_entries_are_in_the_order_they_were_written(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    """Ordered by entry id: not by account id (the first entry written is for the account with
    the higher id), and not debit before credit (the credit is written first)."""
    low = make_account(app_conn, "funding:usd", allow_negative=True)
    high = make_account(app_conn, "customer:high")
    assert low.id < high.id
    tx = post(app_conn, "k1", [Entry(high.id, 100, "USD"), Entry(low.id, -100, "USD")])

    result = get_transfer(app_conn, tx)

    assert [entry.account_id for entry in result.entries] == [high.id, low.id]
    assert [entry.id for entry in result.entries] == sorted(entry.id for entry in result.entries)


def test_a_transfer_with_several_entries_and_currencies_is_complete(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    usd = make_account(app_conn, "funding:usd", "USD", allow_negative=True)
    eur = make_account(app_conn, "funding:eur", "EUR", allow_negative=True)
    alice = make_account(app_conn, "customer:alice", "USD")
    bob = make_account(app_conn, "customer:bob", "EUR")
    tx = post(
        app_conn,
        "k1",
        [
            Entry(usd.id, -300, "USD"),
            Entry(alice.id, 300, "USD"),
            Entry(eur.id, -70, "EUR"),
            Entry(bob.id, 70, "EUR"),
        ],
    )

    result = get_transfer(app_conn, tx)

    assert [(e.account_id, e.amount_minor, e.currency) for e in result.entries] == [
        (usd.id, -300, "USD"),
        (alice.id, 300, "USD"),
        (eur.id, -70, "EUR"),
        (bob.id, 70, "EUR"),
    ]


def test_each_transfer_gets_only_its_own_entries(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    bob = make_account(app_conn, "customer:bob")
    first = post(app_conn, "k1", [Entry(funding.id, -500, "USD"), Entry(alice.id, 500, "USD")])
    second = post(app_conn, "k2", [Entry(alice.id, -200, "USD"), Entry(bob.id, 200, "USD")])

    assert [e.amount_minor for e in get_transfer(app_conn, first).entries] == [-500, 500]
    assert [e.amount_minor for e in get_transfer(app_conn, second).entries] == [-200, 200]


def test_a_reversal_shows_the_transfer_it_reverses(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    original = post(app_conn, "k1", [Entry(funding.id, -500, "USD"), Entry(alice.id, 500, "USD")])
    reversal = post(
        app_conn,
        "k2",
        [Entry(funding.id, 500, "USD"), Entry(alice.id, -500, "USD")],
        reverses=original,
    )

    assert get_transfer(app_conn, reversal).reverses_transaction_id == original
    assert get_transfer(app_conn, original).reverses_transaction_id is None


# --- created_at -------------------------------------------------------------------------------


def test_created_at_is_the_stored_time_as_an_aware_utc_datetime(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    before = datetime.now(UTC) - timedelta(minutes=1)
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    stored = app_conn.execute("SELECT created_at FROM transactions WHERE id = %s", (tx,)).fetchone()
    assert stored is not None

    created_at = get_transfer(app_conn, tx).created_at

    assert created_at.utcoffset() == timedelta(0)
    assert created_at == stored[0]
    assert before < created_at < datetime.now(UTC) + timedelta(minutes=1)


def test_created_at_is_utc_whatever_time_zone_the_session_uses(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    app_conn.execute(
        "SET LOCAL TIME ZONE 'Asia/Tokyo'"
    )  # undone when the test's work is rolled back
    stored = app_conn.execute("SELECT created_at FROM transactions WHERE id = %s", (tx,)).fetchone()
    assert stored is not None
    assert stored[0].utcoffset() == timedelta(hours=9)  # the session really is in Tokyo time

    created_at = get_transfer(app_conn, tx).created_at

    assert created_at.utcoffset() == timedelta(0)
    assert created_at == stored[0]  # the same instant


# --- what it is not ---------------------------------------------------------------------------


def test_an_unknown_transfer_raises_unknown_transfer_error(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    with pytest.raises(UnknownTransferError) as failure:
        get_transfer(app_conn, 999_999)

    assert isinstance(failure.value, LedgerError)
    assert "999999" in str(failure.value)


def test_a_transaction_with_no_entries_is_a_bug_and_not_an_empty_transfer(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    """The no-entries trigger stops this from ever committing, so seeing one means something is
    badly wrong. It must not come back as a transfer with an empty list, and it is not a 404."""
    claim = claim_transaction(app_conn, "k1", hash_for("k1"))

    with pytest.raises(LedgerInvariantError) as failure:
        get_transfer(app_conn, claim.transaction_id)

    assert not isinstance(failure.value, LedgerError)


def test_it_refuses_an_autocommit_connection(
    clean_db: None, settings: Settings, get_transfer
) -> None:
    """Every service function that is given a connection assumes a transaction is in progress."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            get_transfer(conn, 1)


def test_it_changes_nothing(clean_db: None, app_conn: psycopg.Connection, get_transfer) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])
    before = [
        app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        for table in ("transactions", "entries", "balances", "accounts")
    ]
    balances_before = app_conn.execute("SELECT * FROM balances ORDER BY account_id").fetchall()

    get_transfer(app_conn, tx)
    get_transfer(app_conn, tx)

    after = [
        app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        for table in ("transactions", "entries", "balances", "accounts")
    ]
    assert after == before
    assert app_conn.execute("SELECT * FROM balances ORDER BY account_id").fetchall() == (
        balances_before
    )


def test_a_transfer_is_a_frozen_value(
    clean_db: None, app_conn: psycopg.Connection, get_transfer
) -> None:
    funding = make_account(app_conn, "funding:usd", allow_negative=True)
    alice = make_account(app_conn, "customer:alice")
    tx = post(app_conn, "k1", [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")])

    result = get_transfer(app_conn, tx)

    assert result == get_transfer(app_conn, tx)
    assert isinstance(result.entries, tuple)
    assert hash(result) == hash(get_transfer(app_conn, tx))
    with pytest.raises(AttributeError):
        result.id = 1  # type: ignore[misc]
