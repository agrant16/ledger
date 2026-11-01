"""A real deadlock is retried through run_in_transaction (docs/design.md, Milestone 4; ADR 0017).

Two opposing transfers, A to B and B to A, are fired together by a barrier. The locker is the
test-only one from `bad_locking`, which takes the rows in request order and waits between its two
locks, so the pair deadlocks on the first attempt (40P01) and the database aborts one of them.
This time each transfer goes through `run_in_transaction` with `claim_and_post`, so the loser's
whole request runs again, claim included. The locker waits at the barrier on a thread's first call
only, so the retry does not wait for a partner that has finished.

- Both transfers succeed, and the locker was called three times: two first attempts and exactly
  one retry. That shows a real 40P01 happened and was retried. After it the shared invariant check
  passes and the balances and entry count match what was sent (rules 1, 3, 4).
- Each key exists once in `transactions`, and replaying either key afterwards returns the same
  transfer and moves nothing (rule 5): the retry redid the key claim on a fresh transaction.

Postgres waits `deadlock_timeout` (1 second) before it looks for a cycle, so each test takes about
a second.
"""

import random
import threading
from collections.abc import Callable

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.infra.db import run_in_transaction
from ledger.models.posting_model import LockedAccount
from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.repository import posting_repo
from ledger.service.account_service import create_account
from ledger.service.transfer_service import claim_and_post
from tests.concurrency import harness
from tests.concurrency.bad_locking import LockAccounts, lock_in_request_order
from tests.concurrency.helpers import balance_of, count_entries
from tests.db_helpers import commit_transfer, hash_for
from tests.ledger_invariants import assert_ledger_consistent

STARTING_BALANCE = 1_000_000
MAX_AMOUNT = 1_000
FUNDING_ENTRIES = 4  # two funding transfers of two entries each


@pytest.fixture
def service_pool(settings):
    """The pool `run_in_transaction` takes its connection from, apart from the harness's pool."""
    with ConnectionPool(settings.app_url, min_size=2, max_size=2, timeout=5, open=True) as pool:
        yield pool


class CountingLocker:
    """Wraps a locker and counts its calls, which are the attempts that reached the locks."""

    def __init__(self, locker: LockAccounts) -> None:
        self.locker = locker
        self.calls = 0
        self._guard = threading.Lock()

    def __call__(
        self, conn: psycopg.Connection, account_ids: list[int]
    ) -> dict[int, LockedAccount]:
        with self._guard:
            self.calls += 1
        return self.locker(conn, account_ids)


def open_accounts(pool: ConnectionPool) -> tuple[int, int]:
    """Two protected accounts, each funded far above anything the test spends."""
    with pool.connection() as conn:
        funding = create_account(conn, "funding:usd", "USD", True)
        a = create_account(conn, "customer:a", "USD", False)
        b = create_account(conn, "customer:b", "USD", False)
    commit_transfer(pool, "fund:a", funding.id, a.id, STARTING_BALANCE)
    commit_transfer(pool, "fund:b", funding.id, b.id, STARTING_BALANCE)
    return a.id, b.id


def retrying_call(
    service_pool: ConnectionPool, key: str, payload: TransferPayload
) -> Callable[[psycopg.Connection], Transfer]:
    """A call that runs the claim and the posting through `run_in_transaction`, with the retry.

    The harness hands each call its own connection; this one takes its connection from
    `service_pool` instead, because `run_in_transaction` is the unit under test.
    """

    def call(_harness_conn: psycopg.Connection) -> Transfer:
        return run_in_transaction(
            service_pool, lambda conn: claim_and_post(conn, key, hash_for(key), payload)
        )

    return call


def fire_opposing_pair(
    harness_pool: ConnectionPool,
    service_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    seed: int,
) -> tuple[CountingLocker, dict[str, TransferPayload], list[harness.Outcome[Transfer]]]:
    """Opens two accounts and sends A to B and B to A together through the deadlocking locker."""
    rng = random.Random(seed)
    a, b = open_accounts(harness_pool)
    barrier = threading.Barrier(2, timeout=harness.BARRIER_TIMEOUT_SECONDS)
    locker = CountingLocker(lock_in_request_order(barrier, first_call_only=True))
    monkeypatch.setattr(posting_repo, "lock_accounts", locker)

    payloads = {
        "retry:a-to-b": TransferPayload(a, b, rng.randint(1, MAX_AMOUNT), "USD"),
        "retry:b-to-a": TransferPayload(b, a, rng.randint(1, MAX_AMOUNT), "USD"),
    }
    calls = [retrying_call(service_pool, key, payload) for key, payload in payloads.items()]
    return locker, payloads, harness.run_concurrently(harness_pool, calls)


@pytest.mark.timeout(60)
def test_a_real_deadlock_is_retried_and_both_transfers_commit(
    concurrency_pool: ConnectionPool,
    service_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
    monkeypatch: pytest.MonkeyPatch,
):
    locker, payloads, outcomes = fire_opposing_pair(
        concurrency_pool, service_pool, monkeypatch, seed
    )

    failures = [repr(outcome.error) for outcome in outcomes if not outcome.ok]
    assert failures == []
    assert locker.calls == 3, "expected two first attempts and exactly one retry"
    assert_ledger_consistent(owner_conn)
    assert count_entries(owner_conn) == FUNDING_ENTRIES + 2 * len(payloads)
    to_b, to_a = payloads["retry:a-to-b"], payloads["retry:b-to-a"]
    net_to_a = to_a.amount_minor - to_b.amount_minor
    a, b = to_b.from_account_id, to_b.to_account_id
    assert balance_of(owner_conn, a) == STARTING_BALANCE + net_to_a
    assert balance_of(owner_conn, b) == STARTING_BALANCE - net_to_a


@pytest.mark.timeout(60)
def test_the_retried_key_exists_once_and_replays_without_moving_money(
    concurrency_pool: ConnectionPool,
    service_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """Rule 5: the retry claimed its key again on a fresh transaction, and did not leave the first
    claim behind or apply the transfer twice."""
    locker, payloads, outcomes = fire_opposing_pair(
        concurrency_pool, service_pool, monkeypatch, seed
    )
    assert all(outcome.ok for outcome in outcomes), [outcome.error for outcome in outcomes]
    assert locker.calls == 3
    entries_before = count_entries(owner_conn)
    balances_before = {
        account: balance_of(owner_conn, account)
        for payload in payloads.values()
        for account in (payload.from_account_id, payload.to_account_id)
    }

    for (key, payload), outcome in zip(payloads.items(), outcomes, strict=True):
        rows = owner_conn.execute(
            "SELECT count(*) FROM transactions WHERE idempotency_key = %s", (key,)
        ).fetchone()
        assert rows == (1,), key

        replay = run_in_transaction(
            service_pool, lambda conn, k=key, p=payload: claim_and_post(conn, k, hash_for(k), p)
        )
        assert outcome.value is not None
        assert replay.id == outcome.value.id

    assert locker.calls == 3, "a replay must not reach the locks"
    assert count_entries(owner_conn) == entries_before
    for account, balance in balances_before.items():
        assert balance_of(owner_conn, account) == balance
    assert_ledger_consistent(owner_conn)
