# ADR 0017: A deadlock is retried, and a lock wait is bounded, inside `run_in_transaction`

* **Status:** Accepted
* **Date:** 2026-10-30 (decided during milestone 4 planning, issue #197)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
Every write locks its `balances` rows in ascending account id (ADR 0006), so two ordinary transfers cannot wait on each other in a cycle, and a test with a deliberately wrong locker shows what happens without that order. Postgres can still pick a transaction as a deadlock victim (SQLSTATE 40P01) for a cycle the ordering does not cover, and when it does, the whole transaction has been rolled back and the transfer did not happen. Re-running it is safe, provided the idempotency key claim is redone with the posting on a fresh transaction (rule 5), so a retry must re-run the whole `work` function and not part of it.

ADR 0011 expected the retry loop to wrap `run_in_transaction`. This ADR records where it went instead and the other choices around it. It also covers the lock timeout: a request stuck behind another transaction's lock, such as an open claim of the same idempotency key, holds its pool connection for as long as it waits, so the wait needs a bound.

## Decision
- **The retry loop is inside `run_in_transaction`.** The connection is acquired once. Each attempt is one `conn.transaction()` on that connection, with the commit inside the attempt, so a deadlock at any point rolls back that attempt and the next one starts clean.
- **Only 40P01 is retried,** and only for a write. Any other error, including a 40001 serialization failure, propagates as before, because nothing else here is known to be safe to run again. A read-only run is never retried: a read takes no row locks, so a deadlock in one is a bug, and it raises `LedgerInvariantError` (a logged 500) chained to the `DeadlockDetected`.
- **Up to 3 attempts, with a random delay of 10 to 50 ms between attempts.** The delay is a uniform random draw so that retries from different requests do not line up, and there is none after the last attempt. The values are module constants for now and move into `Settings` with the other tunables (milestone 4).
- **Exhausting the attempts raises `ServiceBusyError`,** chained to the last `DeadlockDetected` and with a message that names the deadlock (ADR 0016). The API maps it to the `service-busy` 503 with `Retry-After`.
- **Every write transaction starts with `lock_timeout` of 2 seconds,** set inside the per-attempt `conn.transaction()` with `set_config('lock_timeout', %s, true)`. That is `SET LOCAL` with a bound parameter, which `SET LOCAL` itself does not accept, and it ends with the transaction, so a pooled connection never carries it to its next user. The value is a module constant for now and moves into `Settings` with the other tunables (milestone 4).
- **A read-only run does not get it.** A read takes no row locks to wait on, and `SET TRANSACTION READ ONLY` stays the first statement.
- **A lock timeout (55P03) raises `ServiceBusyError`,** chained to the `LockNotAvailable` and with a message that names the lock timeout (ADR 0016). It is not retried: the holder that blocked this attempt would most likely block the next one, and the request would only hold its connection longer. The transaction rolled back, so the client can retry, with the same idempotency key.
- **The 2 seconds bound each lock wait, not the request.** `lock_timeout` restarts for every statement that waits, so a transaction with several waits can take longer in total. The default `deadlock_timeout` of 1 second is shorter, so a real deadlock is detected and retried before the timeout can fire.

## Consequences
* **Positive:**
  - No route changes. The retry has one home, next to the transaction handling of ADR 0011.
  - The one-connection rule (ADR 0004) holds across retries: a retry never asks the pool for a second connection, and never waits for the pool in the middle of a request.
  - The claim and the posting are redone together, so a retried transfer cannot apply twice or leave a claim behind.

* **Negative / Trade-offs:**
  - `run_in_transaction` now owns the transaction boundaries (`conn.transaction()` per attempt, so the commit and the rollback happen there and not when the pool's context manager exits; the pool context still returns the connection), so it is more code than ADR 0011 described.
  - `work` can run up to 3 times, so it must be safe to run again: no side effects outside the database transaction. Today none has any.
  - A request that waits for a lock holds its pool connection for up to 2 seconds per wait, which counts against the pool and the concurrency cap. The load test (milestone 7) should check what this costs.
  - A retried request holds its pool connection through the delays, which are short next to the pool wait. The load test (milestone 7) should check what this costs: three attempts hold a connection for up to about 100 ms of delay, plus the time of the attempts themselves.
  - A deadlock raised at commit is covered by the same `except`, but no test raises one there; the tests raise from inside `work`.

* **Alternatives Considered:**
  - **A retry loop around `run_in_transaction` (ADR 0011's plan):** each attempt would take a connection from the pool again, which can wait or time out in the middle of a retry, and a caller could forget the wrapper. Rejected.
  - **Retrying in the route or the service function:** every route would carry it, and a partial retry could miss the key claim. Rejected.
  - **No lock timeout, or a longer one:** a stuck holder would hold a pool connection for every request queued behind it, and the pool wait timeout would then fail unrelated requests. Rejected.
  - **Setting it on the role or the connection:** it would apply to reads and to the owner role's work too, and the value would live outside the code that depends on it. Rejected.
  - **Retrying a lock timeout:** see the decision above. Rejected.
  - **Exponential backoff:** more code for a rare event with three attempts. Rejected until load-test results show a need.
  - **Retrying 40001 as well (SERIALIZABLE):** see ADR 0015, which avoids that class of failure by locking rows at READ COMMITTED.

*Once this ADR is Accepted, do not rewrite its decision. Record a later change as a dated note under the section it affects, or as a new ADR that supersedes this one (see CLAUDE.md).*
