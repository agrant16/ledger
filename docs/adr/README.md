# Architecture Decision Records

Short records of the larger decisions behind the ledger service, in the format of [template.md](template.md). `docs/design.md` is the source of truth for the design; these records explain why the biggest choices were made, what they cost, and what was rejected.

| ADR | Decision | Decided |
| --- | --- | --- |
| [0001](0001-raw-sql-and-plain-sql-migrations.md) | Raw SQL with psycopg, plain-SQL migrations, integer minor units | 2026-10-08 |
| [0002](0002-database-roles-and-least-privilege.md) | Owner and app roles, explicit per-table grants | 2026-10-08 |
| [0003](0003-real-database-testing-and-ci-merge-gate.md) | Real-database tests written first, CI as the merge gate | 2026-10-08 |
| [0004](0004-synchronous-handlers-and-fixed-connection-pool.md) | Synchronous handlers, fixed pool, one connection per request | 2026-10-08 |
| [0005](0005-caller-owned-transactions-and-claim-post-split.md) | Caller-owned transactions, claim and post as two functions | 2026-10-09 |
| [0006](0006-row-locking-and-overdraft-check.md) | Ascending row locks at READ COMMITTED, overdraft check on the locked balance | 2026-10-09 |
| [0007](0007-error-taxonomy.md) | Expected failures versus bugs, status mapping in the API layer | 2026-10-09 |
| [0008](0008-defense-in-depth-and-property-testing.md) | Layered enforcement of the invariants, state-machine property test | 2026-10-10 |
| [0009](0009-api-error-responses-rfc-9457.md) | RFC 9457 error responses, request id, minimal logging | 2026-10-11 |
| [0010](0010-test-layout-by-kind.md) | Tests laid out by kind; only the database tests need a database | 2026-10-12 |
| [0011](0011-request-transaction-handling.md) | One request, one transaction, through a single callable helper | 2026-10-12 |
| [0012](0012-idempotency-claim-and-request-hash.md) | The idempotency claim resolves a repeated key in one place, with a canonical request hash | 2026-10-12 |
| [0013](0013-layered-architecture.md) | A layered source tree: API, services, repositories, models and infrastructure | 2026-10-13 |

Decisions still to come are noted where they belong: the SERIALIZABLE write-up (milestone 4).
