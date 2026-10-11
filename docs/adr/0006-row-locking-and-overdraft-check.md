# ADR 0006: Row locks in ascending account order at READ COMMITTED, with the overdraft check on the locked balance

* **Status:** Accepted
* **Date:** 2026-10-09 (decided during milestone 2 planning; implemented 2026-10-10)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
Two concurrent transfers must never spend the same money, and transfers in opposite directions (A to B and B to A) must never deadlock. Protected accounts (`allow_negative` false) must never go below zero, while the funding accounts that issue money may. Balance reads must stay cheap, so the ledger keeps a cached balance next to the entries, which remain the source of truth.

## Decision
- **Row locks at the default READ COMMITTED isolation.**
- **One locking statement** takes the touched `balances` rows `FOR UPDATE`, ordered by ascending account id, each distinct account once. It joins `accounts` to return the currency, and uses `FOR UPDATE OF b` so only the `balances` rows are locked.
- **The overdraft check uses the balance that same statement returns.** At READ COMMITTED a locking `SELECT` that had to wait returns the row as the other writer left it, so the check and the update cannot be separated by another writer. A separate earlier `SELECT` must never feed the check.
- **Entries to the same account are netted into one delta** before the check. A protected account fails if `balance + delta < 0`, so spending exactly the balance is allowed. If several accounts would overdraw, the lowest id is reported.
- **A fixed order of checks:** empty and per-currency balance checks, then unknown account, then currency mismatch, then insufficient funds. A rejected post writes nothing.
- **Cached balances are updated in the same database transaction** as the entries. The CHECK on `balances` (`allow_negative OR balance_minor >= 0`) is a backstop, and reaching it signals a bug, not a normal overdraft.
- **Locking lands in milestone 2.** Deadlock retries, `lock_timeout`, and the concurrency tests that prove the ordering follow in milestone 4.

## Consequences
* **Positive:**
  - Opposing transfers take their locks in the same order, so they cannot deadlock.
  - There is no window between reading a balance and spending it.
  - The check order gives a request with several problems the same error every time.
  - The reconciliation job (milestone 6) can verify the cache against the entries.

* **Negative / Trade-offs:**
  - Transfers touching the same account serialize on its `balances` row. A hot account (for example one funding account feeding every transfer) becomes a bottleneck, so the load test seeds balances up front.
  - Deadlocks are prevented between ordinary transfers, not eliminated in theory, so bounded retry stays in the plan for milestone 4.
  - The lock order lives in one SQL statement's `ORDER BY`; dropping it would work in small tables and fail later. Tests turn off index scans so a missing `ORDER BY` fails.
  - Lock behavior is covered in milestone 2 by single-connection tests only; the concurrency tests come in milestone 4.

* **Alternatives Considered:**
  - **SERIALIZABLE isolation:** not chosen. The full write-up of why is a milestone 4 task.
  - **Reading the balance with a plain `SELECT`, then locking:** rejected, since another writer could change the balance between the two.
  - **Locking in request order:** rejected, because opposing transfers could then deadlock.
  - **Deferring locking to milestone 4:** rejected, since the overdraft check needs the locking statement anyway and writing it in order costs nothing extra.
  - **Computing balances from entries on every read:** not chosen. The cached balance is kept in the same transaction and verified against the entries.
