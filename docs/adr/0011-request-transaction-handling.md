# ADR 0011: One request, one transaction, run through a single callable helper

* **Status:** Accepted
* **Date:** 2026-10-12 (decided during milestone 3 planning)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
ADR 0005 makes the caller own the database transaction: service functions take the request's connection and never commit. ADR 0004 says a request uses one connection from a fixed pool for its whole transaction and never takes a second while holding the first. The first routes (milestone 3) are the callers, so something has to open the transaction, commit it, and give the connection back. Two later needs shape it. The deferred constraint triggers (ADR 0008) fire at commit, so a failure there must still happen inside the handler, where the catch-all can turn it into the logged 500 of ADR 0009. And milestone 4's deadlock retry has to run the whole request body again, including the key claim.

## Decision
- **One helper, `run_in_transaction(pool, work, *, read_only=False)`, in `ledger/db.py`.** It takes one connection from the pool and calls `work(conn)`, which does all of the request's database work and returns plain data. It commits before it returns, rolls back and re-raises if `work` raises, and also rolls back and re-raises if the commit itself fails. The connection goes back to the pool in a usable state either way.
- **A route calls it once** with `request.app.state.pool` and builds its response from the data it returns, after the commit.
- **`work` is a function,** so the whole body can be run again. Milestone 4's retry loop will wrap `run_in_transaction` and no route changes.
- **Reads use `read_only=True`,** which issues `SET TRANSACTION READ ONLY` as the first statement of the transaction, so a bug in a read cannot write. psycopg's `conn.read_only` attribute is deliberately not used: on a pooled connection it would stay set for the next request.
- **The connection is never in autocommit mode,** which the service functions require.

## Consequences
* **Positive:**
  - A trigger failure at commit is raised inside the handler, so it becomes a logged 500 with nothing persisted, and the pool is not left with a broken connection. A test proves this without any transfer code.
  - Every route has the same transaction behavior in one place, and the one-connection rule is structural: a function that is handed the connection cannot take a second one.
  - Later cross-cutting work (a `lock_timeout`, the deadlock retry, mapping a pool timeout to a 503) has a single home.

* **Negative / Trade-offs:**
  - Routes pass a function (usually a lambda) and must return plain data from it, never a cursor, because the connection is gone once the helper returns.
  - Reads open and commit a transaction too. It is cheap, and the transaction is read only.

* **Alternatives Considered:**
  - **A FastAPI dependency with `yield`:** rejected. When its exit code runs relative to sending the response depends on the FastAPI version, and a commit failure has to become the 500 before the response is sent.
  - **A context manager (`with transaction(pool) as conn:`):** rejected. A retry cannot run the body of a `with` block again, so milestone 4 would force every route to change.
  - **Autocommit connections with explicit transactions:** rejected, because the service functions require a transaction already in progress.
  - **Transaction handling in middleware:** rejected. It would open a transaction for requests that never touch the database, and it hides where the commit happens.
