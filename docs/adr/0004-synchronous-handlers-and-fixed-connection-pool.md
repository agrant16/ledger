# ADR 0004: Synchronous handlers, a fixed connection pool, and one connection per request

* **Status:** Accepted (partly implemented: the fixed pool exists; the handler thread limit, the concurrency cap, and the 503 mappings arrive in milestones 3 and 4)
* **Date:** 2026-10-08 (decided in `docs/design.md` at project start)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
Every write is one database transaction on one connection. Under load, the service must fail in a bounded, explainable way rather than queue without limit or deadlock itself. The numbers below are starting points to be tuned from load-test results in milestones 4 and 7.

## Decision
- **Synchronous FastAPI handlers.** Concurrency tests still fire parallel requests with threads.
- **`psycopg_pool` with a fixed pool of 10 connections** (`min_size == max_size`, so it never grows). A request that cannot get a connection waits at most 2 seconds, then becomes a 503.
- **The handler thread pool is set to 20 at startup** (FastAPI's default is 40). The extra threads wait on the pool, with that timeout.
- **An overall concurrency cap** (uvicorn's `--limit-concurrency` or a small middleware) rejects requests above it with a 503 and `Retry-After`. The thread limit alone does not bound queueing, because requests beyond 20 in flight wait for a handler thread with no timeout.
- **One connection per request.** A request uses one connection for its whole transaction and never takes a second while holding the first.
- **Settings come from environment variables**, and the values in force are recorded with every benchmark.

## Consequences
* **Positive:**
  - Overload turns into a bounded 503 with a retry hint, not a hang.
  - The one-connection rule rules out pool deadlock by construction.
  - Synchronous code keeps each request's transaction easy to follow.

* **Negative / Trade-offs:**
  - Throughput is limited by the pool: at most 10 requests make database progress at once.
  - Pool size, thread limit, and cap interact and need tuning together.
  - Retrying after a 503 is safe, but it pushes work onto clients.

* **Alternatives Considered:**
  - **Letting the thread pool alone bound concurrency:** rejected. Requests past the limit queue with no timeout, so the cap was added.
  - **Taking a second connection while holding the first:** rejected. With every connection held by a request waiting for another, the pool would deadlock. Service functions take the request's connection as a parameter (see ADR 0005), which makes this structural.
