"""Sanity tests for the concurrency harness (docs/design.md, Milestone 4; ADR 0010).

Every later concurrency test (rules 1, 3, 4 and 6 through `tests/ledger_invariants.py`) rests on
the harness, so it is tested first: if it hid an exception, started threads one after another, or
hung when a thread could not take part, the tests built on it would prove nothing.

- All 50 calls really run at the same time (they pass a second barrier inside the call, which they
  can only do if every thread is running), each on its own connection.
- Outcomes come back in call order; one call raising is collected and does not hide the others.
- A call that returns is committed and one that raises is rolled back, as in a request.
- A pool too small for the calls, and a barrier that cannot fill, fail promptly with the cause and
  never run a call; they do not hang.
- The shared invariant check (tests/ledger_invariants.py) runs on the database after a run.
- The seed comes from `CONCURRENCY_SEED` or is chosen fresh, a bad value is refused, and the `seed`
  fixture records it.
- The connection budget check passes with room to spare and names the numbers when it fails, and
  the pool fixture is fixed at the thread count with a pool timeout above the barrier timeout.
"""

import threading
import time
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.infra.config import Settings
from tests.concurrency import harness
from tests.db_helpers import make_account
from tests.ledger_invariants import assert_ledger_consistent

Call = Callable[[psycopg.Connection], object]


def echo(value: int) -> Call:
    """A call that returns `value` after a round trip to the database."""

    def call(conn: psycopg.Connection) -> object:
        row = conn.execute("SELECT %s::int", (value,)).fetchone()
        assert row is not None
        return row[0]

    return call


def create_account_named(name: str, *, then_raise: bool = False) -> Call:
    """A call that creates an account and, if asked, raises afterwards."""

    def call(conn: psycopg.Connection) -> object:
        make_account(conn, name)
        if then_raise:
            raise ValueError(name)
        return name

    return call


@pytest.fixture
def small_pool(settings: Settings, migrated_database: None) -> Iterator[ConnectionPool]:
    """A pool of two connections with a one-second wait, too small for three calls."""
    with ConnectionPool(settings.app_url, min_size=2, max_size=2, timeout=1, open=True) as pool:
        pool.wait(timeout=10)
        yield pool


def test_all_calls_run_at_the_same_time(concurrency_pool: ConnectionPool):
    count = harness.CONCURRENCY_THREADS
    together = threading.Barrier(count, timeout=harness.BARRIER_TIMEOUT_SECONDS)

    def call(conn: psycopg.Connection) -> bool:
        together.wait()
        return True

    outcomes = harness.run_concurrently(concurrency_pool, [call] * count)

    assert len(outcomes) == count
    assert all(outcome.ok for outcome in outcomes)


def test_calls_that_cannot_all_run_together_are_detected(concurrency_pool: ConnectionPool):
    """Control for the test above: a barrier with one party too many must break every call."""
    count = 3
    together = threading.Barrier(count + 1, timeout=1)

    def call(conn: psycopg.Connection) -> bool:
        together.wait()
        return True

    outcomes = harness.run_concurrently(concurrency_pool, [call] * count)

    assert [type(outcome.error).__name__ for outcome in outcomes] == ["BrokenBarrierError"] * count


def test_each_call_has_its_own_connection(concurrency_pool: ConnectionPool):
    def backend_pid(conn: psycopg.Connection) -> int:
        return conn.info.backend_pid

    count = harness.CONCURRENCY_THREADS
    outcomes = harness.run_concurrently(concurrency_pool, [backend_pid] * count)

    assert len({outcome.value for outcome in outcomes}) == count


def test_outcomes_come_back_in_call_order(concurrency_pool: ConnectionPool):
    outcomes = harness.run_concurrently(concurrency_pool, [echo(i) for i in range(10)])

    assert [outcome.value for outcome in outcomes] == list(range(10))


def test_one_call_raising_does_not_hide_the_others(concurrency_pool: ConnectionPool):
    def raises(conn: psycopg.Connection) -> object:
        raise ValueError("boom")

    outcomes = harness.run_concurrently(concurrency_pool, [echo(0), echo(1), raises, echo(3)])

    assert isinstance(outcomes[2].error, ValueError)
    assert [outcomes[i].value for i in (0, 1, 3)] == [0, 1, 3]
    assert [outcomes[i].error for i in (0, 1, 3)] == [None, None, None]


def test_a_returning_call_commits_and_a_raising_call_rolls_back(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
):
    calls = [create_account_named(f"harness-{i}", then_raise=(i == 3)) for i in range(6)]

    harness.run_concurrently(concurrency_pool, calls)

    names = {row[0] for row in owner_conn.execute("SELECT account_name FROM accounts")}
    assert names == {f"harness-{i}" for i in range(6) if i != 3}


def test_a_pool_too_small_for_the_calls_fails_promptly_and_runs_nothing(small_pool: ConnectionPool):
    ran: list[int] = []

    def call(conn: psycopg.Connection) -> None:
        ran.append(1)

    started = time.monotonic()
    outcomes = harness.run_concurrently(small_pool, [call] * 3)
    elapsed = time.monotonic() - started

    assert sorted(type(outcome.error).__name__ for outcome in outcomes) == [
        "BrokenBarrierError",
        "BrokenBarrierError",
        "PoolTimeout",
    ]
    assert ran == []
    assert elapsed < 5


def test_a_barrier_that_cannot_fill_times_out_and_runs_nothing(
    settings: Settings, migrated_database: None
):
    ran: list[int] = []

    def call(conn: psycopg.Connection) -> None:
        ran.append(1)

    with ConnectionPool(settings.app_url, min_size=2, max_size=2, timeout=10, open=True) as pool:
        pool.wait(timeout=10)
        started = time.monotonic()
        outcomes = harness.run_concurrently(pool, [call] * 3, barrier_timeout=1)
        elapsed = time.monotonic() - started

    assert [type(outcome.error).__name__ for outcome in outcomes] == ["BrokenBarrierError"] * 3
    assert ran == []
    assert elapsed < 8


def test_the_shared_invariant_check_passes_after_a_run(
    concurrency_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    clean_db: None,
):
    calls = [create_account_named(f"checked-{i}") for i in range(10)]

    outcomes = harness.run_concurrently(concurrency_pool, calls)

    assert all(outcome.ok for outcome in outcomes)
    assert_ledger_consistent(owner_conn)


def test_the_seed_comes_from_the_environment_when_set():
    assert harness.choose_seed({harness.SEED_VARIABLE: "12345"}) == 12345


def test_a_fresh_seed_is_chosen_when_none_is_set():
    seed = harness.choose_seed({})

    assert isinstance(seed, int)
    assert seed >= 0


def test_a_seed_that_is_not_a_number_is_refused_naming_the_variable():
    with pytest.raises(ValueError, match=harness.SEED_VARIABLE):
        harness.choose_seed({harness.SEED_VARIABLE: "abc"})


def test_the_seed_fixture_records_the_seed(seed: int, request: pytest.FixtureRequest):
    assert ("seed", seed) in request.node.user_properties


def test_the_connection_budget_passes_with_room_to_spare():
    harness.check_connection_budget(max_connections=100, superuser_reserved=3)


def test_the_connection_budget_names_the_numbers_when_it_fails():
    with pytest.raises(RuntimeError, match="max_connections") as failure:
        harness.check_connection_budget(max_connections=60, superuser_reserved=3)

    assert "57" in str(failure.value)
    assert str(harness.CONCURRENCY_THREADS + harness.OTHER_CONNECTIONS) in str(failure.value)


def test_the_pool_is_fixed_at_the_thread_count_and_waits_longer_than_the_barrier(
    concurrency_pool: ConnectionPool,
):
    assert concurrency_pool.min_size == concurrency_pool.max_size == harness.CONCURRENCY_THREADS
    assert harness.CONCURRENCY_THREADS == 50
    assert concurrency_pool.timeout > harness.BARRIER_TIMEOUT_SECONDS
