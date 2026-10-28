# ADR 0015: Row locks at READ COMMITTED, not SERIALIZABLE

* **Status:** Draft
* **Date:** 2026-10-28
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
ADR 0006 chose row locks at the default READ COMMITTED isolation level and left the full reasoning against SERIALIZABLE to milestone 4, where the concurrency tests prove the locking. ADR 0012 relies on the same level for the idempotency claim. This ADR records why, what it costs, and what would make us choose differently.

The invariant that needs concurrency control is rule 4: an account with `allow_negative` false never goes below zero. It is a statement about one account's balance. Every write that can lower that balance first locks that account's `balances` row (ADR 0006), so the invariant lives on a single locked row.

## Decision
- **Request transactions run at READ COMMITTED,** the PostgreSQL default. The service sets no isolation level of its own. A test (`tests/integration/db/test_isolation_level.py`) reads `transaction_isolation` inside `run_in_transaction`, for a write and for a read-only call, so a changed server or role default fails the build instead of changing the locking behavior unnoticed.
- **A lock plus the check on the returned balance is enough (rule 4, ADR 0006).** At READ COMMITTED, a `SELECT ... FOR UPDATE` that had to wait returns the row as the other writer committed it. The overdraft check uses that returned balance, so no other writer can change the balance between the check and the update (rule 4). The concurrency tests of milestone 4 exercise this directly.
- **The one deliberate exception is a read-only snapshot.** The reconciliation job (milestone 6) runs several statements that must agree, so it opens one read-only REPEATABLE READ transaction. It writes nothing and takes no locks, so none of the costs below apply to it.

## Consequences
* **Positive:**
  - A transfer waits for the lock and then proceeds on the fresh balance. Two transfers that touch the same account queue instead of failing.
  - There is no serialization-failure error class to map, retry or document. The only concurrency failures are the deadlock and lock timeout cases that milestone 4 handles (ADR 0016).
  - The idempotency claim keeps working as ADR 0012 describes. When two requests race on one key, the second insert waits for the first, and the read of the stored row that follows is a new statement that sees what the first request committed.
  - A single statement is already consistent at this level, so the cached-balance read endpoint needs nothing extra.

* **Negative / Trade-offs:**
  - Correctness depends on every writer taking the lock before it reads the balance. The level does not protect a read-then-write that skips the lock, so the locking statement, its `ORDER BY` and the check on its result are load-bearing, and the milestone 4 controls show each test fails when one is removed.
  - Two statements in one transaction can see different committed data. Code that needs several reads to agree must lock what it reads or use a snapshot, as the reconciliation job does.
  - Transfers on the same account still serialize on its `balances` row. This is the hot-account cost from ADR 0006, and a stricter level would not remove it.

* **Alternatives Considered:**
  - **SERIALIZABLE for every write:** rejected. A hot account would produce 40001 failures on ordinary conflicts, and each would need a retry, so the common case would pay for what the lock already does at no cost. Postgres detects dangerous patterns conservatively, so it may abort a transaction even when no real conflict exists, for example one that touches different accounts. The service would need a serialization-failure error, a retry loop and a limit on attempts, with tests for each, and the retry would have to redo the idempotency claim as well.
  - **REPEATABLE READ for every write:** rejected. A `SELECT ... FOR UPDATE` or an `UPDATE` that meets a row another transaction changed and committed after this transaction's snapshot fails with 40001 ("could not serialize access due to concurrent update"), where READ COMMITTED would wait and use the new row. Two transfers on one account would therefore fail instead of queueing. It also breaks the claim in ADR 0012: an `INSERT ... ON CONFLICT DO NOTHING` that meets a key the other request committed after this transaction's snapshot fails with 40001, where READ COMMITTED would skip the insert and let the read that follows see the stored row.
  - **Setting READ COMMITTED explicitly on every transaction:** not chosen. It costs a statement per request for a value that is already the default, and the isolation level test catches a changed default. If the test ever proves too weak (for example a pooler that resets settings), setting it explicitly is the next step.
  - **Optimistic concurrency (a version column, retry on conflict):** rejected. The invariant is one row wide, so a lock is the simpler and cheaper tool, and every conflict would otherwise become a retry.

## What would change this decision
- **An invariant that spans several rows with no single row to lock,** for example a limit on the combined balance of several accounts, or a rule that reads rows it does not write. That is where write skew appears, and SERIALIZABLE (with retries) or a lock on a shared row would be needed.
- **Dropping the cached balance** in favor of computing it from entries. The lock would then have no row to hold, and the overdraft rule would need another serialization point.
- **Evidence that the single-row lock is not the bottleneck we think it is,** together with a cheap retry path. The milestone 7 benchmarks measure the hot-account cost, so this is a number to check there, not a guess.

*Once this ADR is Accepted, do not rewrite its decision. Record a later change as a dated note under the section it affects, or as a new ADR that supersedes this one (see CLAUDE.md).*
