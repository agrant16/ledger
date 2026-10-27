# ADR 0016: Milestone 4 failures stay `service-busy`, except a lost commit, which has its own error

* **Status:** Accepted
* **Date:** 2026-10-27 (decided during milestone 4 planning, issue #169)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
Milestone 4 adds three ways a write can fail with a 503: a lock timeout (SQLSTATE 55P03), deadlock retries that ran out (40P01 on every attempt), and a connection lost during commit. Since #155, `ledger_error_handler` sends `Retry-After` for a `ServiceBusyError` only, so a new error class gets none unless the handler is changed on purpose. The question is whether the three failures are `ServiceBusyError` or have error classes and problem types of their own.

They are not the same kind of failure. After a lock timeout or exhausted deadlock retries, the transaction rolled back and the transfer did not happen. After a lost commit, the commit may have reached the database before the connection died, so the client cannot know whether the transfer happened. `ServiceBusyError` says the request is "safe for retry". For the first two that is true. For the third, a retry without the idempotency key could move money twice, and only the key (rule 5) makes it safe.

## Decision
- **The lock timeout and exhausted deadlock retries are raised as `ServiceBusyError`.** They keep the one `service-busy` problem type, so the handler sends `Retry-After` and logs the "Service was busy" WARNING as it does for a pool timeout. The `detail` text and the log line say which cause it was. The client's action is the same for all three: wait and retry.
- **A connection lost during commit is a new `CommitOutcomeUnknownError(LedgerError)`,** with its own problem type, `commit-outcome-unknown` (title "Commit outcome unknown"), and status 503. It is a sibling of `ServiceBusyError`, not a subclass. Its `detail` tells the client to replay the request with the same idempotency key, which returns the original result if the transfer happened and applies it once if it did not (rule 5).
- **`Retry-After` is set on purpose for `CommitOutcomeUnknownError`,** because replaying is the right next step and the delay gives the database time to recover. The value is settled with the other 503 values in milestone 4. How the handler decides is #178: a field on `ProblemSpec`, not a class check.
- **Only `run_in_transaction` raises it, and only for a failure at the commit step.** A connection lost before the commit is a plain rollback and is not this error. A read never commits a write, so read routes do not list it.

## Consequences
* **Positive:**
  - The one case where the client cannot know the outcome has a type of its own, so a client can tell "retry" from "replay with the same key, or check first" without parsing the `detail`.
  - `ServiceBusyError` keeps its meaning of "nothing happened, safe to retry".
  - The completeness tests need no change in kind: the new class gets a row in the error table, and a test fails if it does not.

* **Negative / Trade-offs:**
  - One more error class, problem type and table row, and a route that commits lists one more error in its OpenAPI responses.
  - The handler can no longer rely on a bare `isinstance(exc, ServiceBusyError)` for `Retry-After`; it has to read the problem spec (#178).
  - Telling "failed at commit" from "failed before commit" needs care in `run_in_transaction`, and a test with a connection killed at the commit.

* **Alternatives Considered:**
  - **All three as `ServiceBusyError`:** the least code, and the handler change is free. Rejected because it hides the one case where the client must replay, and the "safe for retry" docstring would be wrong for it.
  - **A subclass of `ServiceBusyError` for the lost commit:** rejected. `isinstance` checks, including the handler's, would treat it as safe to retry, and it would share the `service-busy` meaning by inheritance.
  - **Three separate classes, one per cause:** rejected. The lock timeout and the exhausted retries are handled identically by a client, so separate types would add surface and no information.

*Once this ADR is Accepted, do not rewrite its decision. Record a later change as a dated note under the section it affects, or as a new ADR that supersedes this one (see CLAUDE.md).*
