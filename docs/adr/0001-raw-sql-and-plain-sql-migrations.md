# ADR 0001: Raw SQL with psycopg and plain-SQL migrations

* **Status:** Accepted
* **Date:** 2026-10-08 (stack chosen in `docs/design.md` at project start; the append-only migration rule and SQLFluff were added 2026-10-10)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
The point of this project is to show that money stays correct under retries, concurrency, and failures. That depends on things an ORM tends to hide: which rows are locked and in what order, which isolation level a statement runs under, and exactly where a database transaction begins and ends. The author has to be able to explain and defend every query, so the data layer must keep those things explicit.

## Decision
- **Raw SQL through psycopg 3, no ORM.** Queries are plain SQL strings with `%s` parameters, living next to the code that uses them.
- **Migrations are numbered plain SQL files** in `migrations/`, applied by a small runner (`ledger.migrate`) as the owner role. Files run in numeric order, each in its own database transaction, and are recorded in `schema_migrations`. Two files with the same number are rejected even when padded differently (`2_` and `002_`). There is no Alembic.
- **Merged migrations are append-only.** Once a migration is merged it is never edited except for formatting and comments; a schema change is a new migration. Migration 005, which renamed `accounts.name` to `accounts.account_name`, is the first example.
- **SQL is linted with SQLFluff** (PostgreSQL dialect) in CI. Its keyword rule means an identifier must not be a SQL keyword, which is why the column is `account_name`.
- **Schema conventions:** amounts are integers in minor units (cents) with a currency code, never floats. Ids are `bigint GENERATED ALWAYS AS IDENTITY`, so the database assigns every id and the app role needs no sequence grants.

## Consequences
* **Positive:**
  - Locking, isolation, and transaction boundaries are visible in the code and easy to explain.
  - No ORM-generated SQL to second-guess; what runs is what is written.
  - The migration history is a readable, reviewable record of how the schema got here.
  - Integer minor units rule out floating-point rounding errors in money.

* **Negative / Trade-offs:**
  - Every query and every row-to-object mapping is written by hand.
  - There is no migration generation and no automatic rollback: a mistake is fixed by a new forward migration.
  - A migration is recorded as applied the first time it succeeds, so editing an unmerged migration after that needs the local dev database reset before the change is seen (this bit migration 006 during development).
  - The keyword rule forced a rename (migration 005) after the first table was already merged.

* **Alternatives Considered:**
  - **An ORM such as SQLAlchemy:** rejected, because it would hide the locking and isolation behavior the project exists to demonstrate.
  - **Alembic for migrations:** not used. A small runner covers numbered, forward-only SQL files.
  - **Java with Spring Boot:** named in the design as the alternative language; Python was chosen.
