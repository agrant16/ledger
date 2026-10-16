# ADR 0005: Caller-owned transactions, and splitting the claim from the posting

* **Status:** Accepted
* **Date:** 2026-10-09 (decided during milestone 2 planning; implemented 2026-10-10)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
A transfer inserts a `transactions` row (which is also the idempotency claim), then checks and locks accounts, then writes entries and balance updates. The design requires that a failure anywhere leaves nothing behind, including the key row: "a failed attempt does not consume the key". Milestone 4 retries a deadlocked request as a whole, "including the key claim". Milestone 3 will add conflict handling and replay to the claim. We had to decide who opens and commits the database transaction, and whether claiming and posting are one function or two.

## Decision
- **Two functions.** `claim_transaction(conn, idempotency_key, request_hash, reverses_transaction_id=None) -> int` inserts the `transactions` row and returns its id. `post_transaction(conn, transaction_id, entries)` validates, locks, and writes. In milestone 2 the claim is a plain insert and a duplicate key raises the raw unique violation; milestone 3 adds `ON CONFLICT` handling and replay to the claim only. The split still holds with `claim_and_post` (milestone 3, `posting_service.py`): it composes the two so the route makes one call inside one transaction, and the reversal in milestone 5 will compose the same two functions with different entries.
- **The caller owns the database transaction.** Service functions take the request's connection, assume a transaction is open on it, and never commit or roll back. The claim and the posting therefore share one transaction.
- **An autocommit guard.** `transactional_service_method` raises `LedgerInvariantError` if a connection is in autocommit mode. `claim_transaction`, `post_transaction`, and `create_account` all call it.
- **Posting is split into small steps** (`_lock_accounts`, `_insert_entries`, `_apply_balance_deltas`), which gives failure injection a seam: a test replaces `_apply_balance_deltas` with one that raises, then checks that a rollback leaves nothing.
- **Plain data types.** `Entry(account_id, amount_minor, currency)` is a frozen dataclass, and `create_account` returns a frozen `Account`.

## Consequences
* **Positive:**
  - A failure in the posting rolls back the claim as well, so the key is not burned.
  - The transaction boundary sits above the claim, which is what the milestone 4 retry loop needs.
  - A function that only receives a connection cannot take a second one, so ADR 0004's one-connection rule is structural.
  - Milestone 3 changes only the claim; `post_transaction` stays as it is, and milestone 5's reversals reuse both functions.
  - The deferred constraint triggers (ADR 0008) fire at the caller's commit.

* **Negative / Trade-offs:**
  - Atomicity depends on the caller doing the right thing; the autocommit guard catches only the most obvious misuse.
  - A `transactions` row with no entries exists briefly between the two calls. The deferred no-entries trigger covers a caller that commits in that state.
  - Exceptions raised by the commit itself, such as a trigger violation, surface outside these functions, so the milestone 3 error mapping must handle them.

* **Alternatives Considered:**
  - **One function that claims and posts (`post_transaction(conn, entries, *, idempotency_key, request_hash, ...)`):** rejected. It would mix idempotency into the core posting function and change its signature when milestone 3 arrives.
  - **Synthetic keys inside `post_transaction`:** rejected. It would put fabricated keys in the real table and still change the signature later.
  - **`post_transaction` opening and committing its own transaction:** rejected. With the claim separate, the claim would commit on its own, and a posting failure would then burn the key.
  - **A savepoint inside `post_transaction`:** rejected. The request-level rollback already gives the same guarantee, and a savepoint adds a round trip.
  - **A test-only hook parameter, or a temporary database trigger, for failure injection:** not chosen. Monkeypatching a named step needs no test code in production.
