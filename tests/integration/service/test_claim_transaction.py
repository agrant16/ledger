"""claim_transaction (docs/design.md, Error behavior and Request hash; ADR 0012): the idempotency
claim.

    claim_transaction(conn, idempotency_key, request_hash, reverses_transaction_id=None) -> Claim
    Claim(transaction_id, replay)

It inserts the transactions row for a new key and returns its id with replay=False. If the key is
already there with the same hash, it inserts nothing and returns the original id with replay=True.
If the key is there with a different hash, it raises IdempotencyConflictError. A key claimed by a
transaction that has not committed yet makes the second claim wait for that transaction to finish.

It takes the request's connection and never commits; the caller owns the transaction, so a rollback
frees the key again.
"""

import dataclasses
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.accounts import create_account
from ledger.errors import IdempotencyConflictError, LedgerInvariantError
from ledger.infra.config import Settings
from tests.db_helpers import hash_for, wait_until_waiting_for_a_lock

HASH_A = hash_for("request a")
HASH_B = hash_for("request b")


@pytest.fixture
def claim_transaction():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.posting import claim_transaction

    return claim_transaction


@pytest.fixture
def claim_type():
    from ledger.models.posting_model import Claim

    return Claim


def transaction_rows(conn: psycopg.Connection) -> list[tuple[int, str, str, int | None]]:
    return conn.execute(
        "SELECT id, idempotency_key, request_hash, reverses_transaction_id FROM transactions"
        " ORDER BY id"
    ).fetchall()


# --- a first claim ------------------------------------------------------------------------------


def test_a_new_key_returns_a_claim_that_is_not_a_replay(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction, claim_type
) -> None:
    claim = claim_transaction(app_conn, "key-1", HASH_A)

    assert isinstance(claim, claim_type)
    assert claim.replay is False
    assert claim.transaction_id >= 1


def test_each_new_key_gets_a_new_id(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    first = claim_transaction(app_conn, "key-1", HASH_A)
    second = claim_transaction(app_conn, "key-2", HASH_B)

    assert second.transaction_id > first.transaction_id
    assert second.replay is False


def test_a_claim_is_a_frozen_value(claim_type) -> None:
    claim = claim_type(transaction_id=7, replay=True)

    assert claim == claim_type(7, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.replay = False  # type: ignore[misc]


def test_stores_the_key_the_hash_and_no_reversal_by_default(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    claim = claim_transaction(app_conn, "key-1", HASH_A)

    assert transaction_rows(app_conn) == [(claim.transaction_id, "key-1", HASH_A, None)]


def test_stores_the_transaction_a_reversal_points_at(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    original = claim_transaction(app_conn, "key-1", HASH_A)
    reversal = claim_transaction(
        app_conn, "key-2", HASH_B, reverses_transaction_id=original.transaction_id
    )

    rows = transaction_rows(app_conn)
    assert rows[1] == (reversal.transaction_id, "key-2", HASH_B, original.transaction_id)


def test_the_same_hash_under_a_new_key_is_a_new_claim(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """The key is the identity of a request. Two keys may carry identical requests."""
    first = claim_transaction(app_conn, "key-1", HASH_A)
    second = claim_transaction(app_conn, "key-2", HASH_A)

    assert second.replay is False
    assert second.transaction_id != first.transaction_id


@pytest.mark.parametrize("other_key", ["KEY-1", "key-1 ", " key-1", "key-1\N{NO-BREAK SPACE}"])
def test_keys_are_compared_exactly(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction, other_key: str
) -> None:
    first = claim_transaction(app_conn, "key-1", HASH_A)
    second = claim_transaction(app_conn, other_key, HASH_A)

    assert second.replay is False
    assert second.transaction_id != first.transaction_id


# --- the same key again: a replay ---------------------------------------------------------------


def test_the_same_key_and_hash_is_a_replay_of_the_original(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction, claim_type
) -> None:
    first = claim_transaction(app_conn, "key-1", HASH_A)

    again = claim_transaction(app_conn, "key-1", HASH_A)

    assert again == claim_type(first.transaction_id, True)


@pytest.mark.parametrize("replayed_key, request_hash", [("key-1", HASH_A), ("KEY-1", HASH_B)])
def test_a_replay_finds_the_row_for_exactly_its_own_key(
    clean_db: None,
    app_conn: psycopg.Connection,
    claim_transaction,
    replayed_key: str,
    request_hash: str,
) -> None:
    """Keys that differ only in case are two claims, and each replays its own row."""
    lower = claim_transaction(app_conn, "key-1", HASH_A)
    upper = claim_transaction(app_conn, "KEY-1", HASH_B)
    expected = {"key-1": lower, "KEY-1": upper}[replayed_key]

    again = claim_transaction(app_conn, replayed_key, request_hash)

    assert again.replay is True
    assert again.transaction_id == expected.transaction_id


def test_a_replay_writes_nothing(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    first = claim_transaction(app_conn, "key-1", HASH_A)
    before = transaction_rows(app_conn)

    claim_transaction(app_conn, "key-1", HASH_A)
    claim_transaction(app_conn, "key-1", HASH_A)

    assert transaction_rows(app_conn) == before == [(first.transaction_id, "key-1", HASH_A, None)]


def test_the_transaction_is_still_usable_after_a_replay(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """A violated unique constraint would abort the whole database transaction. A replay must not:
    the caller goes on to read the stored transfer in the same transaction."""
    claim_transaction(app_conn, "key-1", HASH_A)
    claim_transaction(app_conn, "key-1", HASH_A)

    assert app_conn.execute("SELECT 1").fetchone() == (1,)
    assert claim_transaction(app_conn, "key-2", HASH_B).replay is False


def test_a_replay_of_a_reversal_is_a_replay_and_not_a_unique_violation(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """A retried reversal repeats both the key and the reversal link, and Postgres does not say
    which unique constraint it reports. The claim resolves the key first, so it is a replay."""
    original = claim_transaction(app_conn, "key-1", HASH_A)
    reversal = claim_transaction(
        app_conn, "key-2", HASH_B, reverses_transaction_id=original.transaction_id
    )

    again = claim_transaction(
        app_conn, "key-2", HASH_B, reverses_transaction_id=original.transaction_id
    )

    assert again.replay is True
    assert again.transaction_id == reversal.transaction_id


def test_a_second_reversal_under_a_new_key_is_not_taken_for_a_replay(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """Only the key conflict is a replay. A different key aimed at an already reversed transfer
    must still fail on the reversal link. Milestone 5 turns this into AlreadyReversedError and
    updates this test."""
    original = claim_transaction(app_conn, "key-1", HASH_A)
    claim_transaction(app_conn, "key-2", HASH_B, reverses_transaction_id=original.transaction_id)

    with pytest.raises(psycopg.errors.UniqueViolation):
        claim_transaction(
            app_conn, "key-3", hash_for("another"), reverses_transaction_id=original.transaction_id
        )


# --- the same key with a different request: a conflict ------------------------------------------


def test_the_same_key_with_a_different_hash_is_a_conflict(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    claim_transaction(app_conn, "key-1", HASH_A)

    with pytest.raises(IdempotencyConflictError):
        claim_transaction(app_conn, "key-1", HASH_B)


def test_a_conflict_changes_nothing_and_leaves_the_transaction_usable(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    first = claim_transaction(app_conn, "key-1", HASH_A)

    with pytest.raises(IdempotencyConflictError):
        claim_transaction(app_conn, "key-1", HASH_B)

    assert transaction_rows(app_conn) == [(first.transaction_id, "key-1", HASH_A, None)]
    assert claim_transaction(app_conn, "key-2", HASH_B).replay is False


def test_a_conflict_message_does_not_repeat_the_key_or_the_hashes(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """The message becomes the response detail, so it is fixed text."""
    claim_transaction(app_conn, "secret-key-123", HASH_A)

    with pytest.raises(IdempotencyConflictError) as failure:
        claim_transaction(app_conn, "secret-key-123", HASH_B)

    message = str(failure.value)
    assert message
    assert "secret-key-123" not in message
    assert HASH_A not in message
    assert HASH_B not in message


# --- the transaction belongs to the caller ------------------------------------------------------


def test_it_never_commits(
    clean_db: None,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    claim_transaction,
) -> None:
    """The caller owns the transaction, so a rollback must undo the claim."""
    claim_transaction(app_conn, "key-1", HASH_A)
    assert app_conn.execute("SELECT count(*) FROM transactions").fetchone() == (1,)
    app_conn.rollback()
    assert owner_conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)


def test_a_rolled_back_claim_frees_the_key_for_any_request(
    clean_db: None, app_conn: psycopg.Connection, claim_transaction
) -> None:
    """A failed attempt does not consume the key: a retry, even a different request, claims it
    fresh."""
    claim_transaction(app_conn, "key-1", HASH_A)
    app_conn.rollback()

    retry = claim_transaction(app_conn, "key-1", HASH_B)

    assert retry.replay is False
    assert transaction_rows(app_conn)[0][1:3] == ("key-1", HASH_B)


def test_it_refuses_an_autocommit_connection(
    clean_db: None, settings: Settings, owner_conn: psycopg.Connection, claim_transaction
) -> None:
    """The claim and the posting must share one transaction, so a failure rolls back both."""
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            claim_transaction(conn, "key-1", HASH_A)
    assert owner_conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)


# --- two requests with the same key at once -----------------------------------------------------


@pytest.fixture
def funded(app_conn: psycopg.Connection, commit_setup):
    """Committed accounts, so claims on other connections can post a real transfer and commit."""
    funding = create_account(app_conn, "funding:usd", "USD", True)
    alice = create_account(app_conn, "customer:alice", "USD", False)
    commit_setup()
    return funding, alice


def claim_and_post(conn: psycopg.Connection, funded, key: str, request_hash: str):
    """Claim a key and post a transfer on it, leaving the transaction open."""
    from ledger.models.posting_model import Entry

    from ledger.posting import claim_transaction, post_transaction

    funding, alice = funded
    claim = claim_transaction(conn, key, request_hash)
    post_transaction(
        conn,
        claim.transaction_id,
        [Entry(funding.id, -500, "USD"), Entry(alice.id, 500, "USD")],
    )
    return claim


def second_claim_waits_for_the_first(
    owner_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    funded,
    second_hash: str,
    first_finishes,
):
    """The first connection claims "key-1" and holds it open. The second connection claims the same
    key and must wait. Then `first_finishes(conn)` commits or rolls back the first transaction.
    Returns what the first claim was and what the second claim returned or raised."""
    from ledger.posting import claim_transaction

    with app_pool.connection() as first_conn, app_pool.connection() as second_conn:
        first = claim_and_post(first_conn, funded, "key-1", HASH_A)
        with ThreadPoolExecutor(max_workers=1) as executor:
            outcome = executor.submit(claim_transaction, second_conn, "key-1", second_hash)
            wait_until_waiting_for_a_lock(owner_conn, second_conn.info.backend_pid, outcome.done)
            assert not outcome.done()  # still waiting while the first holds the key

            first_finishes(first_conn)

            try:
                result = outcome.result(timeout=5)
            except IdempotencyConflictError as failure:
                result = failure
        second_conn.rollback()
    return first, result


def test_a_second_claim_waits_and_then_replays_a_committed_first(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool, funded
) -> None:
    first, second = second_claim_waits_for_the_first(
        owner_conn, app_pool, funded, HASH_A, lambda conn: conn.commit()
    )

    assert first.replay is False
    assert second.replay is True
    assert second.transaction_id == first.transaction_id


def test_a_second_claim_waits_and_then_takes_over_a_rolled_back_first(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool, funded
) -> None:
    """The first request failed and rolled back, so its key was never consumed. The waiting
    request claims it for itself, even though its request differs."""
    first, second = second_claim_waits_for_the_first(
        owner_conn, app_pool, funded, HASH_B, lambda conn: conn.rollback()
    )

    assert second.replay is False
    assert second.transaction_id != first.transaction_id


def test_a_second_claim_waits_and_then_conflicts_with_a_committed_first_of_another_request(
    owner_conn: psycopg.Connection, app_pool: ConnectionPool, funded
) -> None:
    first, second = second_claim_waits_for_the_first(
        owner_conn, app_pool, funded, HASH_B, lambda conn: conn.commit()
    )

    assert isinstance(second, IdempotencyConflictError)
