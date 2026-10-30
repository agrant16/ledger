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
- A second control for a wrong `ORDER BY`: the real locking statement with its order replaced by
  the order of the request. A removed `ORDER BY` is not caught by the real test, because two rows
  come back in id order anyway, so this shows that a statement that follows request order does
  deadlock under the same scenario.
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
from tests.concurrency import harness
from tests.concurrency.bad_locking import LOCK_IN_REQUEST_ORDER_SQL, lock_in_request_order
from tests.concurrency.helpers import balance_of, count_entries, transfer_call
from tests.db_helpers import commit_transfer
from tests.ledger_invariants import assert_ledger_consistent

Call = Callable[[psycopg.Connection], Transfer]

DEADLOCK_SQLSTATE = "40P01"
STARTING_BALANCE = 1_000_000
MAX_AMOUNT = 1_000
ROUNDS = 20
# Pairs of opposing transfers per round: 25 pairs are the 50 threads of the harness pool.
PAIRS_PER_ROUND = harness.CONCURRENCY_THREADS // 2
CONTROL_ROUNDS = 3
WRONG_ORDER_PAIRS = 3
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
            calls.append(transfer_call(key, payload))
            net_to_a += amount if target == a else -amount
    rng.shuffle(calls)
    return calls, net_to_a


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

        failures = [repr(outcome.error) for outcome in outcomes if not outcome.ok]
        assert failures == [], f"round {round_number}: {failures}"
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

        assert count_entries(owner_conn) == FUNDING_ENTRIES + 2 * (round_number + 1)
        assert_ledger_consistent(owner_conn)


@pytest.mark.timeout(60)
def test_one_statement_locking_in_request_order_deadlocks_opposing_transfers(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """Control for a wrong `ORDER BY`: a single locking statement that follows request order.

    There is no barrier between the locks, so a deadlock is likely in a round but not certain.
    The test runs up to `ROUNDS` rounds of a few threads, stops at the first deadlock, and needs
    at least one 40P01 and no other failure.
    """
    rng = random.Random(seed)
    a, b = open_accounts(concurrency_pool)
    monkeypatch.setattr(posting_repo, "LOCK_ACCOUNTS_SQL", LOCK_IN_REQUEST_ORDER_SQL)
    deadlocks = 0

    for round_number in range(ROUNDS):
        calls, _ = opposing_calls(a, b, WRONG_ORDER_PAIRS, rng, f"wrong-order-{round_number}")

        outcomes = harness.run_concurrently(concurrency_pool, calls)

        failed = [outcome.error for outcome in outcomes if not outcome.ok]
        assert all(isinstance(error, errors.DeadlockDetected) for error in failed), repr(failed)
        deadlocks += len(failed)
        assert_ledger_consistent(owner_conn)
        if deadlocks:
            break

    assert deadlocks > 0, f"no deadlock in {ROUNDS} rounds"
