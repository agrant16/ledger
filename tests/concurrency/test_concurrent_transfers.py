"""50 concurrent transfers across a few accounts never overdraw and never change the total
(docs/design.md, Milestone 4; rule 4). Defends rules 1, 3 and 4.

One funding account and four protected accounts holding 1000 each. 50 threads, one transfer each,
straight at the service layer with no retry: 20 withdrawals to the funding account and 30 transfers
between protected accounts, from random sources, in random order. The withdrawals add up to more
than the 4000 the protected accounts hold, and only withdrawals take money out of them, so at
least one withdrawal is refused in every run.

After each run:
- every transfer succeeded or raised `InsufficientFundsError`; a `LedgerInvariantError` would mean
  the overdraft check let one through to the database constraint;
- every balance equals its start plus the entries of the transfers that succeeded, no protected
  balance is negative, and all balances still sum to zero;
- the entry count matches the successful transfers, and the shared invariant check passes.

Run 20 times as 20 parametrized runs, each with its own logged seed (`seed` fixture).
"""

import random

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.errors import InsufficientFundsError
from ledger.models.transfer_model import TransferPayload
from ledger.service.account_service import create_account
from tests.concurrency import harness
from tests.concurrency.helpers import Call, balance_of, count_entries, transfer_call
from tests.db_helpers import commit_transfer
from tests.ledger_invariants import assert_ledger_consistent

RUNS = 20
PROTECTED_ACCOUNTS = 4
STARTING_BALANCE = 1_000
PROTECTED_TOTAL = PROTECTED_ACCOUNTS * STARTING_BALANCE
WITHDRAWALS = 20
INTERNAL_TRANSFERS = harness.CONCURRENCY_THREADS - WITHDRAWALS
# Twenty withdrawals of at least this much add up to more than the protected accounts hold.
MIN_WITHDRAWAL = PROTECTED_TOTAL // WITHDRAWALS + 1
MAX_AMOUNT = 600
FUNDING_ENTRIES = 2 * PROTECTED_ACCOUNTS  # one funding transfer of two entries per account


def open_accounts(pool: ConnectionPool) -> tuple[int, list[int]]:
    """The funding account and four protected accounts, each funded with 1000."""
    with pool.connection() as conn:
        funding = create_account(conn, "funding:usd", "USD", True)
        protected = [
            create_account(conn, f"customer:{n}", "USD", False).id
            for n in range(PROTECTED_ACCOUNTS)
        ]
    for n, account_id in enumerate(protected):
        commit_transfer(pool, f"fund:{n}", funding.id, account_id, STARTING_BALANCE)
    return funding.id, protected


def build_calls(funding: int, protected: list[int], rng: random.Random) -> list[Call]:
    """20 withdrawals to funding and 30 transfers between protected accounts, shuffled."""
    payloads = [
        TransferPayload(
            rng.choice(protected), funding, rng.randint(MIN_WITHDRAWAL, MAX_AMOUNT), "USD"
        )
        for _ in range(WITHDRAWALS)
    ]
    for _ in range(INTERNAL_TRANSFERS):
        source, target = rng.sample(protected, 2)
        payloads.append(TransferPayload(source, target, rng.randint(1, MAX_AMOUNT), "USD"))
    rng.shuffle(payloads)
    return [transfer_call(f"transfer:{n}", payload) for n, payload in enumerate(payloads)]


@pytest.mark.timeout(60)
@pytest.mark.parametrize("run", range(RUNS))
def test_50_concurrent_transfers_never_overdraw_or_change_the_total(
    run: int,
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
    seed: int,
):
    rng = random.Random(seed)
    funding, protected = open_accounts(concurrency_pool)
    calls = build_calls(funding, protected, rng)

    outcomes = harness.run_concurrently(concurrency_pool, calls)

    refused = [outcome for outcome in outcomes if not outcome.ok]
    unexpected = [
        repr(outcome.error)
        for outcome in refused
        if not isinstance(outcome.error, InsufficientFundsError)
    ]
    assert unexpected == []
    succeeded = [outcome.value for outcome in outcomes if outcome.ok]
    assert succeeded and refused, f"{len(succeeded)} succeeded, {len(refused)} refused"

    expected = {funding: -PROTECTED_TOTAL, **dict.fromkeys(protected, STARTING_BALANCE)}
    for transfer in succeeded:
        assert transfer is not None
        for entry in transfer.entries:
            expected[entry.account_id] += entry.amount_minor
    for account_id, balance in expected.items():
        assert balance_of(owner_conn, account_id) == balance, f"account {account_id}"
    assert all(balance_of(owner_conn, account_id) >= 0 for account_id in protected)

    total = owner_conn.execute("SELECT sum(balance_minor) FROM balances").fetchone()
    assert total is not None and total[0] == 0
    assert count_entries(owner_conn) == FUNDING_ENTRIES + 2 * len(succeeded)
    assert_ledger_consistent(owner_conn)
