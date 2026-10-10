"""claim_transaction (docs/design.md, milestone 2): insert the transactions row, return its id.

It takes the request's connection and never commits; the caller owns the transaction. Milestone 3
adds the conflict handling that makes it the idempotency claim.
"""

import psycopg
import pytest

from ledger.config import Settings
from ledger.errors import LedgerInvariantError


@pytest.fixture
def claim_transaction():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.posting import claim_transaction

    return claim_transaction


def test_returns_the_new_transaction_id(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    first = claim_transaction(app_conn, "key-1", "hash-1")
    second = claim_transaction(app_conn, "key-2", "hash-2")
    assert first >= 1
    assert second > first


def test_stores_the_key_the_hash_and_no_reversal_by_default(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    tx = claim_transaction(app_conn, "key-1", "hash-1")
    row = app_conn.execute(
        "SELECT idempotency_key, request_hash, reverses_transaction_id FROM transactions"
        " WHERE id = %s",
        (tx,),
    ).fetchone()
    assert row == ("key-1", "hash-1", None)


def test_stores_the_transaction_a_reversal_points_at(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    original = claim_transaction(app_conn, "key-1", "hash-1")
    reversal = claim_transaction(app_conn, "key-2", "hash-2", reverses_transaction_id=original)
    row = app_conn.execute(
        "SELECT reverses_transaction_id FROM transactions WHERE id = %s", (reversal,)
    ).fetchone()
    assert row == (original,)


def test_it_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    claim_transaction,
) -> None:
    """The caller owns the transaction, so a rollback must undo the claim."""
    claim_transaction(app_conn, "key-1", "hash-1")
    assert app_conn.execute("SELECT count(*) FROM transactions").fetchone() == (1,)
    app_conn.rollback()
    assert owner_conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)


def test_it_refuses_an_autocommit_connection(
    clean_db: None, settings: Settings, owner_conn: psycopg.Connection, claim_transaction
) -> None:
    """The claim and the posting must share one transaction, so a failure rolls back both."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            claim_transaction(conn, "key-1", "hash-1")
    assert owner_conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
