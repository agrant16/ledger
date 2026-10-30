"""Two transfers that together exceed a balance: one succeeds, one is refused (docs/design.md,
Milestone 4; rule 4, ADR 0006).

A has 100. A to B for 70 and A to C for 70 are fired together by a barrier, straight at the service
layer with no retry. The targets differ, so the only contended row is A's `balances` row. Defends
rules 1, 3 and 4.

- The real code: exactly one transfer succeeds and the other raises `InsufficientFundsError`,
  because the check uses the balance returned by the locking statement. A ends at 30.
- The negative control: a test-only locker that reads the balance with a plain SELECT before
  locking. Both transfers pass the check on the stale 100. The second then waits for the row lock
  and its balance update hits `balances_protected_nonnegative`, which the repository reports as
  `LedgerInvariantError`. So the clean refusal comes from the locked read, and the constraint is
  only the last line of defence.
"""

import threading

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import ConnectionPool

from ledger.errors import InsufficientFundsError, LedgerInvariantError
from ledger.models.transfer_model import TransferPayload
from ledger.repository import posting_repo
from ledger.service.account_service import create_account
from tests.concurrency import harness
from tests.concurrency.helpers import Call, balance_of, count_entries, transfer_call
from tests.concurrency.stale_balance import read_before_locking
from tests.db_helpers import commit_transfer, reset_database
from tests.ledger_invariants import assert_ledger_consistent

ROUNDS = 20
STARTING_BALANCE = 100
AMOUNT = 70
FUNDING_ENTRIES = 2  # one funding transfer of two entries
CONSTRAINT = "balances_protected_nonnegative"


def open_accounts(pool: ConnectionPool) -> tuple[int, int, int]:
    """A protected account A holding 100, and two empty protected accounts B and C."""
    with pool.connection() as conn:
        funding = create_account(conn, "funding:usd", "USD", True)
        a = create_account(conn, "customer:a", "USD", False)
        b = create_account(conn, "customer:b", "USD", False)
        c = create_account(conn, "customer:c", "USD", False)
    commit_transfer(pool, "fund:a", funding.id, a.id, STARTING_BALANCE)
    return a.id, b.id, c.id


def overspending_calls(a: int, b: int, c: int, label: str) -> list[Call]:
    """A to B and A to C for 70 each, which together exceed A's 100."""
    return [
        transfer_call(f"{label}:b", TransferPayload(a, b, AMOUNT, "USD")),
        transfer_call(f"{label}:c", TransferPayload(a, c, AMOUNT, "USD")),
    ]


@pytest.mark.timeout(60)
def test_two_transfers_exceeding_the_balance_give_one_success_and_one_refusal(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
):
    for round_number in range(ROUNDS):
        reset_database(owner_conn)
        a, b, c = open_accounts(concurrency_pool)
        calls = overspending_calls(a, b, c, f"round-{round_number}")

        outcomes = harness.run_concurrently(concurrency_pool, calls)

        failed = [outcome.error for outcome in outcomes if not outcome.ok]
        assert sum(outcome.ok for outcome in outcomes) == 1, f"round {round_number}: {failed}"
        assert len(failed) == 1
        assert isinstance(failed[0], InsufficientFundsError), repr(failed[0])
        assert balance_of(owner_conn, a) == STARTING_BALANCE - AMOUNT
        assert balance_of(owner_conn, b) + balance_of(owner_conn, c) == AMOUNT
        assert count_entries(owner_conn) == FUNDING_ENTRIES + 2
        assert_ledger_consistent(owner_conn)


@pytest.mark.timeout(60)
def test_checking_a_balance_read_before_locking_is_stopped_only_by_the_constraint(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
):
    """Control for the test above: the locked read is what produces the clean refusal."""
    for round_number in range(ROUNDS):
        reset_database(owner_conn)
        a, b, c = open_accounts(concurrency_pool)
        after_read = threading.Barrier(2, timeout=harness.BARRIER_TIMEOUT_SECONDS)
        calls = overspending_calls(a, b, c, f"control-{round_number}")

        # Patched only for the run, so the next round's funding does not wait at the barrier.
        with monkeypatch.context() as patch:
            patch.setattr(posting_repo, "lock_accounts", read_before_locking(after_read))
            outcomes = harness.run_concurrently(concurrency_pool, calls)

        failed = [outcome.error for outcome in outcomes if not outcome.ok]
        assert sum(outcome.ok for outcome in outcomes) == 1, f"round {round_number}: {failed}"
        assert len(failed) == 1
        error = failed[0]
        assert isinstance(error, LedgerInvariantError), repr(error)
        assert isinstance(error.__cause__, errors.CheckViolation), repr(error.__cause__)
        assert error.__cause__.diag.constraint_name == CONSTRAINT
        assert balance_of(owner_conn, a) == STARTING_BALANCE - AMOUNT
        assert count_entries(owner_conn) == FUNDING_ENTRIES + 2
        assert_ledger_consistent(owner_conn)
