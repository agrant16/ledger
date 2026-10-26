# ADR 0004: Synchronous handlers, a fixed connection pool, and one connection per request

* **Status:** Accepted (partly implemented: the fixed pool, the handler thread limit, the concurrency cap and the pool-timeout 503 exist; the lock timeout, exhausted deadlock retries and lost-commit 503 mappings arrive in milestone 4)
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

*Note (2026-10-19): `GET /health` is a readiness check, so a pool wait timeout there is the same 503 `service-busy` with `Retry-After` as on any other route, not a 500 (PR #92, #91). A liveness check, `/livez`, is a deferred decision (#94).*

*Note (2026-10-20): the concurrency cap is a small ASGI middleware, not uvicorn's `--limit-concurrency` (issue #98, PR #99). It is read from `CONCURRENCY_CAP` (default 40, twice the thread limit, so some waiting is allowed before requests are rejected) and counts every in-flight HTTP request, `/health` included. It sits inside the request-id middleware, so its 503 carries a request id, and it answers with the same `service-busy` problem and a `Retry-After` header before any transaction starts. The rejection is logged at WARNING. One INFO line at startup records the pool size, pool timeout, thread limit and cap.*

*Note (2026-10-20): settings are loaded once, by the app factory `create_app(settings=None)` in `ledger/main.py`, and stored on `app.state.settings` (issue #100, PR #113). The concurrency cap middleware, the thread limit, the pool and the startup INFO line all read that one object, so the cap that is logged is the cap that is enforced. `app = create_app()` stays at module level for `uvicorn ledger.main:app`, and tests build their own apps with `create_app(Settings(...))`. A bad value stops the app when it is built, with an error naming the variable. `Settings` is a `BaseSettings`, so a field that is not passed is still read from the environment; a test that must not depend on the environment passes every field it cares about. The decision itself is unchanged; the design doc (Stack decisions, Settings) holds the detail.*

*Note (2026-10-26, #155): `GET /health` no longer takes a connection from the pool, so the 2026-10-19 note above no longer holds: a pool wait timeout there is not a 503 `service-busy`. The check opens its own short-lived connection with its own timeouts (`HEALTH_TIMEOUT_SECONDS`, default 2), so a pool with every connection in use cannot make the service unready; the body reports `"pool": "saturated"` with a 200, and overload is left to the concurrency cap and `Retry-After`. A failed check is a 503 `not-ready` problem without `Retry-After`. The decision itself (synchronous handlers, a fixed pool, one connection per request) is unchanged; the design doc (Error behavior) holds the detail.*

*Note (2026-10-26, #155): the concurrency cap no longer counts `/health`, so it can never reject a readiness probe. This replaces the part of the 2026-10-20 note that says it counts every in-flight HTTP request, `/health` included. The startup INFO line also records the readiness timeout.*

*Note (2026-10-26, #155): `GET /health` runs its check on a thread limiter of its own, `HEALTH_CONCURRENCY` (default 2), and not on the handler threads, so a burst of probes cannot take the handler threads and traffic cannot make a probe wait for one; exempting it from the cap did not bound that, because the route still ran on the shared 20. The route is therefore `async` and hands the blocking check to that limiter. A probe that arrives while every health slot is busy waits for one with no timeout of its own; whether to bound that wait is a deferred decision (#168). The startup INFO line also records `health_concurrency`. The decision itself (synchronous handlers, a fixed pool, one connection per request) is unchanged.*

*Note (2026-10-26, #155): `Retry-After` and the "Service was busy" WARNING are tied to the error class, not to the 503 status. `ledger_error_handler` used to send them for every 503; it now does so for `ServiceBusyError` only, which is what lets `NotReadyError` be a 503 without `Retry-After`. The failures that this ADR's status line lists for milestone 4 (the lock timeout, exhausted deadlock retries and a lost commit connection) therefore get no `Retry-After` unless they are raised as `ServiceBusyError` or the handler is changed. Whether they are `ServiceBusyError` or their own error classes is open and is a decision item in milestone 4 of the design doc. The decision itself is unchanged.*

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
