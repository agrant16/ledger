# ADR 0012: The idempotency claim resolves a repeated key in one place, with a canonical request hash

* **Status:** Draft
* **Date:** 2026-10-12 (decided during milestone 3 planning)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
A client that does not get a response to `POST /transfers` retries with the same `Idempotency-Key`, and the retry must never move money twice. ADR 0005 split a transfer into `claim_transaction`, which inserts the `transactions` row and so claims the key, and `post_transaction`, which writes the entries. Until now the claim was a plain insert, so a repeated key surfaced as a raw unique violation. Several things have to be settled before the API can use it. The claim must tell a retry of the same request from a reuse of the key for a different request. Two requests with the same key can arrive at the same moment. A failed first attempt must not use the key up. And the stored hash has to be something that stays the same for harmless differences between retries and never silently changes meaning, because every stored hash is compared with new ones.

## Decision
- **The claim returns `Claim(transaction_id, replay)`,** a frozen dataclass in `ledger/posting.py`. A new key gives `replay=False`. The same key with the same hash gives the original id with `replay=True`, and nothing is written. The same key with a different hash raises `IdempotencyConflictError` (409). Its message is fixed text and never repeats the key or the hashes.
- **One statement decides it:** `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. When no row comes back, a second statement reads the stored row for that key and compares hashes. The conflict target names the key only, so a conflict on the reversal link (`reverses_transaction_id`) is not mistaken for a replay and still raises the unique violation. Resolving the key first is also what makes a retried reversal a replay, since that retry repeats both unique values and Postgres does not say which one it reports. Milestone 5 turns the reversal-link violation into `AlreadyReversedError`.
- **Concurrent requests with one key** need nothing extra. If the first request has not committed, Postgres makes the second insert wait for it. When the first commits, the second sees the row and replays or conflicts. When the first rolls back, the second inserts its own row. The read of the stored row is a new statement at READ COMMITTED, so it sees what the first request committed.
- **A failed attempt does not use the key,** because the key row and the transfer share one database transaction (ADR 0005, ADR 0011). A rollback removes both.
- **The hash is the SHA-256, as 64 lowercase hex digits, of the canonical JSON of `[METHOD, path, body]`:** keys sorted, no spaces, the method uppercased, the path the request was routed on (so a reversal's target is part of it), and the body the validated request model dumped to JSON (so a default written out hashes the same as one left out), or `null` when there is no body. The function is `request_hash(method, path, body)` in `ledger/api/request_hash.py`. Text is not normalized: two strings the database treats as different names are different requests.
- **Migration 007 adds `transactions_request_hash_format`,** a CHECK that `request_hash` is 64 lowercase hex digits. The hash is computed by our own code, so the CHECK is a safety net against a bug and not a validation of client input. It is listed in the CHECK-completeness test as a service rule.

## Consequences
* **Positive:**
  - A repeated key is handled in one function, in one round trip for a new key and two for a repeated one, with no savepoint and no aborted transaction to recover from.
  - The retry behavior needs no locks of its own and no isolation level above READ COMMITTED: the unique index on the key is the lock.
  - The hash form is pinned by a test, so it cannot change by accident. A stored hash from an older release still compares correctly.
  - The reversal case and the failed-then-retried case are covered by the same statement.

* **Negative / Trade-offs:**
  - A repeated key costs a second statement, because the app role has no UPDATE grant on `transactions` and so cannot use `ON CONFLICT DO UPDATE ... RETURNING`.
  - The hash form is a contract. Any change to it, even a harmless one, would make every stored key look like a conflict, so it needs a migration plan and a new ADR.
  - A request is "the same" only if its validated body is the same. Two bodies that mean the same thing to a human but differ as data (a different account name spelling, a different currency case) are different requests, which matches the strict validation.

* **Alternatives Considered:**
  - **Catch the `UniqueViolation` and then read the row:** rejected. The violation aborts the database transaction, so the read would need a savepoint around every claim, and the violation does not say which unique constraint fired.
  - **`ON CONFLICT DO UPDATE SET ... RETURNING` to get the row back in one statement:** rejected, because the app role has no UPDATE privilege on `transactions` (ADR 0002), and granting it would weaken the insert-only rule.
  - **A separate idempotency table with a stored response:** rejected. A replay is rebuilt from the stored transaction and its entries, so there is no second copy of the response to keep in step.
  - **Hashing the raw request bytes:** rejected, because key order and spacing differ between retries from different clients, and a default written out explicitly would look like a different request.
  - **SERIALIZABLE isolation for the claim:** rejected. The unique index already serializes two claims of one key. The isolation-level write-up for the transfer itself belongs to milestone 4.
