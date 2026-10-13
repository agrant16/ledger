# ADR 0002: Two database roles and least-privilege grants

* **Status:** Accepted
* **Date:** 2026-10-08 (roles created during scaffolding; per-table grants written with each table from 2026-10-09)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
The ledger's strongest guarantees are about what can never happen: entries are never changed or deleted, an account's currency never changes, and a request hash cannot be rewritten after the fact. If the service connected with broad privileges, those rules would depend only on every line of application code behaving. Privileges let the database refuse these operations itself.

## Decision
- **Two roles.** `ledger_owner` owns the database and every table; migrations and test fixtures connect as it. `ledger_app` is what the service connects as; it owns nothing. The service never connects as the superuser or the owner.
- **Explicit grants, in the migration that creates the table.** There is no `ALTER DEFAULT PRIVILEGES` anywhere.
- **Grants per table for the app role:**

| Table | App role privileges |
| --- | --- |
| `accounts` | `SELECT`, `INSERT` (currency and `allow_negative` are fixed for life) |
| `transactions` | `SELECT`, `INSERT` (key, hash, and reversal link cannot change) |
| `entries` | `SELECT`, `INSERT` (append-only: no `UPDATE`, `DELETE`, or `TRUNCATE`) |
| `balances` | `SELECT`, `INSERT`, `UPDATE` (a cache, updated with every posting) |
| `schema_migrations` | none |

- **Role setup is one SQL file**, `docker/roles/roles.sql`. Docker Compose runs it from an init script; CI runs the same file with `psql`. Since milestone 3 it also creates the separate `ledger_test` database the tests run in, with the same owner and grants (see ADR 0003).
- **A test pins the grants.** It asserts every table's exact privileges for the app role, rejects column-level grants, and fails if a table is missing from its list, so a stray grant fails the build.

## Consequences
* **Positive:**
  - The database itself refuses `UPDATE`, `DELETE`, and `TRUNCATE` on entries, whatever the application does.
  - Immutable columns on `accounts` and `transactions` need no application code to stay immutable.
  - Because the app role has no `UPDATE` on `accounts`, a locking query can only lock `balances` rows, not account rows.
  - Drift in privileges is caught in CI.

* **Negative / Trade-offs:**
  - Tests need a second, owner-role connection to reset and seed data.
  - Every new table needs an explicit grant and an entry in the privileges test.
  - Two sets of credentials to configure locally and in CI.

* **Alternatives Considered:**
  - **A single role:** nothing but application code would then stop a bug from rewriting entries. Rejected.
  - **`ALTER DEFAULT PRIVILEGES`:** rejected explicitly, because grants should be visible next to the table they apply to.
