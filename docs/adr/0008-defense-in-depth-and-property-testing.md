# ADR 0008: Defense in depth for the ledger invariants, proven by a state-machine property test

* **Status:** Accepted
* **Date:** 2026-10-10 (the layers were in the design from 2026-10-08; the triggers and the property test were built in milestone 2)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
The ledger has invariants that must always hold: each transaction's entries sum to zero per currency, entries are never changed, a cached balance equals the sum of its entries, a protected account is never negative, and every entry's currency matches its account's. Application checks can have bugs, and example-based tests only cover cases someone thought of.

## Decision
- **Each invariant is enforced in more than one place.** `post_transaction` checks first. The database then backs it up with:
  - composite foreign keys, so an entry's currency must match its account's and a balance row's `allow_negative` must match the account's;
  - a CHECK on `balances` against a negative protected balance;
  - privileges that refuse `UPDATE` and `DELETE` on entries (ADR 0002);
  - **deferred constraint triggers** (migration 006): one on `entries` checks that a transaction's entries sum to zero within each currency, and one on `transactions` rejects a transaction with no entries. Both are `DEFERRABLE INITIALLY DEFERRED`, so they fire when the database transaction commits. A claim followed by a posting still commits as one unit. Both raise `check_violation`.
- **The property test is a Hypothesis `RuleBasedStateMachine`** on a real database. It builds random sequences of account openings, funding transfers, balanced posts in one or two currencies, boundary spends, and five kinds of deliberately broken posts. It checks every invariant after every step, keeps an in-memory model of the balances, and requires that a rejected post leave the entries, transactions, and balances exactly as they were.
- **Accepted posts really commit, and the database is emptied when each example starts and ends.** Savepoints cannot be used, because nothing inside one is committed and the triggers would never fire.
- **Tests are proven able to fail.** The property test was run against eleven deliberately broken versions of `posting.py` (since split into `service/posting_service.py` and `repository/posting_repo.py`, ADR 0013) and caught ten. The eleventh, a missing row lock, needs the concurrency tests of milestone 4.

## Consequences
* **Positive:**
  - A bug in one layer cannot silently corrupt the ledger; it surfaces as a database error.
  - Random sequences find combinations no example test covers, and a failure shrinks to a short sequence.
  - The triggers are tested directly by committing unbalanced and empty transactions.

* **Negative / Trade-offs:**
  - The zero-sum trigger runs once per entry row at commit. For a two-entry transfer that is two small indexed lookups; the cost is measured in milestone 7 by running the load test with and without it.
  - Tests that commit entries must be balanced, which is why `app_conn` rolls back at teardown.
  - The property test takes about 10 seconds because every accepted post commits.
  - The test uses one connection at a time, so it cannot find concurrency bugs.

* **Alternatives Considered:**
  - **Application checks only:** rejected. A bug there would be a corrupt ledger.
  - **Immediate (non-deferred) triggers:** rejected, because the first entry of a transaction would already be unbalanced and fail.
  - **A single `@given` test over a list of operations:** a state machine was chosen for per-step invariants and better shrinking.
  - **Savepoints or per-example transactions for isolation:** rejected, since triggers fire only at commit.
