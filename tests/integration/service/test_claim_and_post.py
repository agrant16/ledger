"""claim_and_post (ledger/service/posting_service.py; docs/design.md, Error behavior, Correctness
rules 1 to 6; ADR 0012 and ADR 0013).

    claim_and_post(conn, idempotency_key, request_hash, payload: TransferPayload) -> Transfer

This is the whole body of POST /transfers inside the request's transaction: claim the key, post
the two entries unless the claim is a replay, and return the stored transfer, read back from the
repository (`posting_repo.find_transfer`). A transfer that is missing or has no entries at that
point is a bug (LedgerInvariantError), not an UnknownTransferError. The tests call it with keyword
arguments, so the parameter order is free. It takes the request's connection, never commits, and
refuses an autocommit connection like the functions it calls (`claim_transaction` and
`post_transaction` stay separate, because the reversal in milestone 5 calls them with different
entries).

The fixed order of checks (design doc, Error behavior), seen from the service:

    1. the key claim: a replay returns the original transfer, the same key with a different request
       hash is IdempotencyConflictError, and both end the request there;
    2. unknown account (UnknownAccountError), then currency mismatch (CurrencyMismatchError);
    3. the lock and the funds check (InsufficientFundsError).

A failure at 2 or 3 leaves the key unclaimed once the caller rolls back, so the same key can be
used again and is evaluated fresh. These tests commit through `run_in_transaction`, as the route
does, so a failed call rolls back and a successful one commits for real. The database is reset
before and after each test.
"""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.errors import (
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientFundsError,
    LedgerError,
    LedgerInvariantError,
    UnknownAccountError,
    UnknownTransferError,
)
from ledger.infra.config import Settings
from ledger.infra.db import run_in_transaction
from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.repository import posting_repo
from ledger.service.posting_service import claim_and_post
from ledger.service.transfer_service import get_transfer
from tests.db_helpers import commit_accounts, commit_transfer, hash_for, reset_database


@pytest.fixture
def world(app_pool: ConnectionPool, owner_conn: psycopg.Connection):
    """Committed funding, alice (funded with 1000) and bob accounts."""
    reset_database(owner_conn)
    funding, alice, bob = commit_accounts(app_pool)
    commit_transfer(app_pool, "fund-alice", funding.id, alice.id, 1000)
    yield funding, alice, bob
    reset_database(owner_conn)


def run(
    pool: ConnectionPool,
    key: str,
    from_account_id: int,
    to_account_id: int,
    amount_minor: int,
    currency: str = "USD",
    request_hash: str | None = None,
) -> Transfer:
    """One request: claim_and_post in its own committed (or rolled back) transaction."""
    return run_in_transaction(
        pool,
        lambda conn: claim_and_post(
            conn,
            idempotency_key=key,
            request_hash=request_hash or hash_for(key),
            payload=TransferPayload(
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                currency=currency,
                amount_minor=amount_minor,
            ),
        ),
    )


def stored(owner_conn: psycopg.Connection) -> tuple[list, list, list]:
    """Everything a transfer can change: transactions, entries and cached balances."""
    return (
        owner_conn.execute(
            "SELECT id, idempotency_key, request_hash FROM transactions ORDER BY id"
        ).fetchall(),
        owner_conn.execute(
            "SELECT id, transaction_id, account_id, amount_minor, currency FROM entries ORDER BY id"
        ).fetchall(),
        owner_conn.execute(
            "SELECT account_id, balance_minor FROM balances ORDER BY account_id"
        ).fetchall(),
    )


def balance(owner_conn: psycopg.Connection, account_id: int) -> int:
    row = owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    return row[0]


# --- a new request -----------------------------------------------------------------------------


def test_a_new_request_posts_two_entries_and_returns_the_stored_transfer(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    _, alice, bob = world

    transfer = run(app_pool, "k1", alice.id, bob.id, 300)

    assert transfer.reverses_transaction_id is None
    assert [(e.account_id, e.amount_minor, e.currency) for e in transfer.entries] == [
        (alice.id, -300, "USD"),
        (bob.id, 300, "USD"),
    ]
    row = owner_conn.execute(
        "SELECT idempotency_key, request_hash FROM transactions WHERE id = %s", (transfer.id,)
    ).fetchone()
    assert row == ("k1", hash_for("k1"))
    assert balance(owner_conn, alice.id) == 700
    assert balance(owner_conn, bob.id) == 300


def test_the_returned_transfer_is_the_one_that_was_committed(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """The response is rebuilt from stored rows, so a later read agrees with it exactly."""
    _, alice, bob = world

    transfer = run(app_pool, "k1", alice.id, bob.id, 300)

    assert run_in_transaction(app_pool, lambda conn: get_transfer(conn, transfer.id)) == transfer


def test_the_entries_sum_to_zero_and_the_cached_balances_equal_the_entries(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Correctness rules 1 and 3 after a posted request."""
    _, alice, bob = world

    transfer = run(app_pool, "k1", alice.id, bob.id, 300)

    assert sum(e.amount_minor for e in transfer.entries) == 0
    for account in (alice, bob):
        entries_sum = owner_conn.execute(
            "SELECT coalesce(sum(amount_minor), 0) FROM entries WHERE account_id = %s",
            (account.id,),
        ).fetchone()
        assert entries_sum == (balance(owner_conn, account.id),)


# --- replay ------------------------------------------------------------------------------------


def test_a_replay_returns_the_original_transfer_and_moves_nothing(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Correctness rule 5: the same key and request never apply twice."""
    _, alice, bob = world
    first = run(app_pool, "k1", alice.id, bob.id, 300)
    after_first = stored(owner_conn)

    again = run(app_pool, "k1", alice.id, bob.id, 300)

    assert again == first
    assert stored(owner_conn) == after_first


def test_a_replay_does_not_check_the_funds_again(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """The claim ends the request: alice has spent everything, and the replay is still the
    original answer, not an insufficient-funds error."""
    _, alice, bob = world
    first = run(app_pool, "k1", alice.id, bob.id, 1000)
    assert balance(owner_conn, alice.id) == 0

    again = run(app_pool, "k1", alice.id, bob.id, 1000)

    assert again == first
    assert balance(owner_conn, alice.id) == 0


# --- the same key with a different request ----------------------------------------------------


def test_the_same_key_with_a_different_request_is_a_conflict_and_moves_nothing(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    _, alice, bob = world
    run(app_pool, "k1", alice.id, bob.id, 300)
    before = stored(owner_conn)

    with pytest.raises(IdempotencyConflictError) as failure:
        run(
            app_pool,
            "k1",
            alice.id,
            bob.id,
            400,
            request_hash=hash_for("a different body"),
        )

    assert "k1" not in str(failure.value)
    assert hash_for("k1") not in str(failure.value)
    assert hash_for("a different body") not in str(failure.value)
    assert stored(owner_conn) == before


def test_the_key_claim_is_checked_before_the_accounts(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Step 1 before step 2: a used key with a different request naming an unknown account is a
    409, not a 404."""
    _, alice, bob = world
    run(app_pool, "k1", alice.id, bob.id, 300)

    with pytest.raises(IdempotencyConflictError):
        run(
            app_pool,
            "k1",
            alice.id,
            bob.id + 1000,
            300,
            request_hash=hash_for("a different body"),
        )


# --- the order of the later checks -------------------------------------------------------------


def test_an_unknown_account_is_checked_before_the_currency(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Step 2: unknown account (404) comes before currency mismatch (400)."""
    _, alice, bob = world

    with pytest.raises(UnknownAccountError):
        run(app_pool, "k1", alice.id, bob.id + 1000, 300, currency="EUR")


def test_a_currency_mismatch_is_checked_before_the_funds(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Steps 2 before 3: a currency mismatch on a transfer that is also too large is a 400."""
    _, alice, bob = world

    with pytest.raises(CurrencyMismatchError):
        run(app_pool, "k1", alice.id, bob.id, 5000, currency="EUR")


def test_insufficient_funds_is_reported_when_nothing_else_is_wrong(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """The control for the order tests above."""
    _, alice, bob = world

    with pytest.raises(InsufficientFundsError) as failure:
        run(app_pool, "k1", alice.id, bob.id, 1500)

    assert failure.value.account_id == alice.id
    assert failure.value.balance_minor == 1000
    assert failure.value.net_change_minor == -1500


# --- failed attempts do not burn the key -------------------------------------------------------

FAILURES = [
    pytest.param(
        {"to_offset": 1000, "amount_minor": 300, "currency": "USD"},
        UnknownAccountError,
        id="unknown-account",
    ),
    pytest.param(
        {"to_offset": 0, "amount_minor": 300, "currency": "EUR"},
        CurrencyMismatchError,
        id="currency-mismatch",
    ),
    pytest.param(
        {"to_offset": 0, "amount_minor": 1500, "currency": "USD"},
        InsufficientFundsError,
        id="insufficient-funds",
    ),
]


@pytest.mark.parametrize(("request_kwargs", "expected_error"), FAILURES)
def test_a_failed_request_stores_nothing_and_leaves_the_key_free(
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    world,
    request_kwargs,
    expected_error,
) -> None:
    """The key row and the transfer share one transaction, so a failure rolls both back. A
    different request with the same key then succeeds instead of being a 409."""
    _, alice, bob = world
    before = stored(owner_conn)

    with pytest.raises(expected_error):
        run(
            app_pool,
            "k1",
            alice.id,
            bob.id + request_kwargs["to_offset"],
            request_kwargs["amount_minor"],
            request_kwargs["currency"],
        )

    assert stored(owner_conn) == before
    other = run(
        app_pool,
        "k1",
        alice.id,
        bob.id,
        100,
        request_hash=hash_for("another request"),
    )
    assert other.entries[0].amount_minor == -100


def test_a_request_that_failed_for_funds_succeeds_once_the_account_is_funded(
    app_pool: ConnectionPool, owner_conn: psycopg.Connection, world
) -> None:
    """Failed-then-retried: the retry with the same key and request is evaluated against the
    balance as it is now."""
    funding, alice, bob = world
    with pytest.raises(InsufficientFundsError):
        run(app_pool, "k1", alice.id, bob.id, 1500)
    commit_transfer(app_pool, "top-up", funding.id, alice.id, 1000)

    transfer = run(app_pool, "k1", alice.id, bob.id, 1500)

    assert [e.amount_minor for e in transfer.entries] == [-1500, 1500]
    assert balance(owner_conn, alice.id) == 500


# --- the read-back (issue #161) -----------------------------------------------------------------
# After the claim and the post, the transfer is read back from the repository. The database keeps a
# missing or empty transfer from happening for real, so these tests make `find_transfer` report
# it. Either way it is a bug in the service, never a client error such as UnknownTransferError.


@pytest.fixture(params=["new", "replay"])
def path(request: pytest.FixtureRequest, app_pool: ConnectionPool, world) -> str:
    """The two ways to reach the read-back: a new claim, or a replay of a committed one."""
    if request.param == "replay":
        _, alice, bob = world
        run(app_pool, "k1", alice.id, bob.id, 300)
    return request.param


def test_a_missing_transfer_after_the_claim_is_a_bug_and_not_an_unknown_transfer(
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    world,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, alice, bob = world
    before = stored(owner_conn)
    asked_for: list[int] = []

    def find_nothing(conn: psycopg.Connection, transfer_id: int) -> None:
        asked_for.append(transfer_id)

    monkeypatch.setattr(posting_repo, "find_transfer", find_nothing)

    with pytest.raises(LedgerInvariantError) as failure:
        run(app_pool, "k1", alice.id, bob.id, 300)

    assert not isinstance(failure.value, (LedgerError, UnknownTransferError))
    assert failure.value.__cause__ is None  # raised as the bug it is, not translated from a 404
    assert len(asked_for) == 1
    assert str(asked_for[0]) in str(failure.value)  # names the transaction id
    assert stored(owner_conn) == before  # a new claim is rolled back; a replay changed nothing


def test_a_transfer_without_entries_after_the_claim_is_still_a_bug(
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    world,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check `get_transfer` makes must still be made on this path."""
    _, alice, bob = world
    before = stored(owner_conn)

    def find_without_entries(conn: psycopg.Connection, transfer_id: int) -> Transfer:
        return Transfer(
            id=transfer_id,
            reverses_transaction_id=None,
            created_at=datetime.now(UTC),
            entries=(),
        )

    monkeypatch.setattr(posting_repo, "find_transfer", find_without_entries)

    with pytest.raises(LedgerInvariantError) as failure:
        run(app_pool, "k1", alice.id, bob.id, 300)

    assert not isinstance(failure.value, LedgerError)
    assert stored(owner_conn) == before


# --- the connection ----------------------------------------------------------------------------


def test_it_refuses_an_autocommit_connection(
    settings: Settings, owner_conn: psycopg.Connection, world
) -> None:
    """The claim and the posting must share one transaction, so a failure rolls back both."""
    _, alice, bob = world
    before = stored(owner_conn)

    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            claim_and_post(
                conn,
                idempotency_key="k1",
                request_hash=hash_for("k1"),
                payload=TransferPayload(
                    from_account_id=alice.id,
                    to_account_id=bob.id,
                    currency="USD",
                    amount_minor=300,
                ),
            )

    assert stored(owner_conn) == before
