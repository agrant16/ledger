"""Runs callables in threads, released together, for the concurrency tests (docs/design.md,
Milestone 4).

`run_concurrently` gives each call its own pooled connection and releases all calls at once with a
barrier, no sleeps. It never raises for a failing call: each call's value or exception comes back
as an `Outcome`, in call order, so a test can assert on all of them. Setting the database up and
checking the ledger invariants afterwards (`tests/ledger_invariants.py`) is left to the test.
"""

import random
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

# Threads in the largest concurrency test, and so the size of the fixed test pool.
CONCURRENCY_THREADS = 50

# How long threads wait for each other at the barrier. The pool waits longer for a free connection,
# so a pool that is too small fails with a pool timeout and not a broken barrier, and both stay
# under the 30-second test timeout.
BARRIER_TIMEOUT_SECONDS = 10
POOL_TIMEOUT_SECONDS = 15

# Connections a test run can hold next to the concurrency pool: the session `app_pool` (up to 10),
# the owner connections (1 to 2), the service's own pool (10) and the Compose app (10), rounded up.
OTHER_CONNECTIONS = 32

SEED_VARIABLE = "CONCURRENCY_SEED"
_SEED_LIMIT = 2**32


@dataclass(frozen=True)
class Outcome[T]:
    """What one call did: the value it returned, or the exception it raised."""

    value: T | None = None
    error: Exception | None = None


def run_concurrently[T](
    pool: ConnectionPool,
    calls: Sequence[Callable[[psycopg.Connection], T]],
    *,
    barrier_timeout: float = BARRIER_TIMEOUT_SECONDS,
) -> list[Outcome[T]]:
    """Runs each call in its own thread, on its own connection, released together.

    Each thread takes a connection from `pool` before the barrier, so a pool that is too small
    fails the run instead of hanging at the barrier. A call that returns is committed; one that
    raises is rolled back, as in a request. If a thread cannot get a connection, the barrier is
    aborted and the other threads fail at once with `BrokenBarrierError`.

    Args:
        pool: The pool to take one connection per call from.
        calls: The work to run, each taking its connection.
        barrier_timeout: Seconds the threads wait for each other before the run fails.

    Returns:
        One `Outcome` per call, in call order.
    """
    if not calls:
        return []
    barrier = threading.Barrier(len(calls), timeout=barrier_timeout)

    def run_one(call: Callable[[psycopg.Connection], T]) -> Outcome[T]:
        try:
            with ExitStack() as stack:
                try:
                    conn = stack.enter_context(pool.connection())
                except Exception:
                    barrier.abort()
                    raise
                barrier.wait()
                return Outcome(value=call(conn))
        except Exception as error:
            return Outcome(error=error)

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        return list(executor.map(run_one, calls))


def choose_seed(environ: Mapping[str, str]) -> int:
    """The seed for a test's random data: `CONCURRENCY_SEED` if set, else a fresh one.

    Raises:
        ValueError: `CONCURRENCY_SEED` is set but is not an integer.
    """
    raw = environ.get(SEED_VARIABLE)
    if raw is None:
        return random.SystemRandom().randrange(_SEED_LIMIT)
    try:
        return int(raw)
    except ValueError as failure:
        raise ValueError(f"{SEED_VARIABLE} must be an integer, got {raw!r}") from failure


def check_connection_budget(max_connections: int, superuser_reserved: int) -> None:
    """Fails when the server cannot hold the concurrency pool next to the other test pools.

    Raises:
        RuntimeError: The usable connections are fewer than the pool and the others need.
    """
    usable = max_connections - superuser_reserved
    needed = CONCURRENCY_THREADS + OTHER_CONNECTIONS
    if usable < needed:
        raise RuntimeError(
            f"The concurrency tests need {needed} connections ({CONCURRENCY_THREADS} for their "
            f"pool and up to {OTHER_CONNECTIONS} for the other pools), but the server allows "
            f"max_connections={max_connections} with {superuser_reserved} reserved, so {usable} "
            "are usable. Raise max_connections on the test database server."
        )
