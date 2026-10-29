"""Opposing transfers never deadlock, and a locker in request order does (docs/design.md,
Milestone 4; ADR 0006).

Transfers A to B and B to A, fired together by a barrier straight at the service layer with no
retry, because a retry would hide a lock-order bug. Defends rules 1, 3 and 4.

- The real code: 50 threads, half each way, over 20 rounds. Every transfer succeeds, so no
  deadlock (40P01) and no other failure. After every round the shared invariant check passes, and
  at the end the balances and the entry count match what was sent.
- The negative control: the same scenario with a test-only locker that takes the rows in request
  order and waits between its two locks. Two opposing transfers then deadlock every time, which
  shows the test above is able to fail. It must fail with 40P01 and nothing else.
"""

import random
import threading
from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import ConnectionPool

from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.repository import posting_repo
from ledger.service.account_service import create_account
from ledger.service.transfer_service import claim_and_post
from tests.concurrency import harness
from tests.concurrency.bad_locking import lock_in_request_order
from tests.db_helpers import commit_transfer, hash_for
from tests.ledger_invariants import assert_ledger_consistent

Call = Callable[[psycopg.Connection], Transfer]

DEADLOCK_SQLSTATE = "40P01"
STARTING_BALANCE = 1_000_000
MAX_AMOUNT = 1_000
ROUNDS = 20
# Pairs of opposing transfers per round: 25 pairs are the 50 threads of the harness pool.
PAIRS_PER_ROUND = harness.CONCURRENCY_THREADS // 2
CONTROL_ROUNDS = 3
FUNDING_ENTRIES = 4  # two funding transfers of two entries each


def open_accounts(pool: ConnectionPool) -> tuple[int, int]:
    """Two protected accounts, each funded far above anything a round can spend."""
    with pool.connection() as conn:
        funding = create_account(conn, "funding:usd", "USD", True)
        a = create_account(conn, "customer:a", "USD", False)
        b = create_account(conn, "customer:b", "USD", False)
    commit_transfer(pool, "fund:a", funding.id, a.id, STARTING_BALANCE)
    commit_transfer(pool, "fund:b", funding.id, b.id, STARTING_BALANCE)
    return a.id, b.id


def opposing_calls(
    a: int, b: int, pairs: int, rng: random.Random, label: str
) -> tuple[list[Call], int]:
    """`pairs` transfers A to B and as many B to A, each with its own key and a seeded amount.

    Returns the calls, in shuffled order, and the net amount they add to A's balance.
    """
    calls: list[Call] = []
    net_to_a = 0
    for pair in range(pairs):
        for source, target in ((a, b), (b, a)):
            amount = rng.randint(1, MAX_AMOUNT)
            key = f"{label}:{pair}:{source}"
            payload = TransferPayload(source, target, amount, "USD")

            def call(conn: psycopg.Connection, key=key, payload=payload) -> Transfer:
                return claim_and_post(conn, key, hash_for(key), payload)

            calls.append(call)
            net_to_a += amount if target == a else -amount
    rng.shuffle(calls)
    return calls, net_to_a


def balance_of(conn: psycopg.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def count_entries(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM entries").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.timeout(90)
def test_opposing_transfers_never_deadlock(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
):
    rng = random.Random(seed)
    a, b = open_accounts(concurrency_pool)
    net_to_a = 0

    for round_number in range(ROUNDS):
        calls, net = opposing_calls(a, b, PAIRS_PER_ROUND, rng, f"round-{round_number}")
        net_to_a += net

        outcomes = harness.run_concurrently(concurrency_pool, calls)

        errors_seen = [repr(outcome.error) for outcome in outcomes if not outcome.ok]
        assert errors_seen == [], f"round {round_number}: {errors_seen}"
        assert_ledger_consistent(owner_conn)

    sent = ROUNDS * PAIRS_PER_ROUND * 2
    assert count_entries(owner_conn) == FUNDING_ENTRIES + 2 * sent
    assert balance_of(owner_conn, a) == STARTING_BALANCE + net_to_a
    assert balance_of(owner_conn, b) == STARTING_BALANCE - net_to_a


@pytest.mark.timeout(60)
def test_locking_in_request_order_deadlocks_opposing_transfers(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """Control for the test above: the lock order is what prevents the deadlock."""
    rng = random.Random(seed)
    a, b = open_accounts(concurrency_pool)
    committed_transfers = 0

    for round_number in range(CONTROL_ROUNDS):
        after_first_lock = threading.Barrier(2, timeout=harness.BARRIER_TIMEOUT_SECONDS)
        monkeypatch.setattr(posting_repo, "lock_accounts", lock_in_request_order(after_first_lock))
        calls, _ = opposing_calls(a, b, 1, rng, f"control-{round_number}")

        outcomes = harness.run_concurrently(concurrency_pool, calls)

        failed = [outcome.error for outcome in outcomes if not outcome.ok]
        assert len(failed) == 1, f"round {round_number}: {failed}"
        assert isinstance(failed[0], errors.DeadlockDetected), repr(failed[0])
        assert failed[0].sqlstate == DEADLOCK_SQLSTATE
        assert sum(outcome.ok for outcome in outcomes) == 1

        committed_transfers += 1
        assert count_entries(owner_conn) == FUNDING_ENTRIES + 2 * committed_transfers
        assert_ledger_consistent(owner_conn)
