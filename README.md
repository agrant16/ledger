# Ledger Service

A small double-entry ledger service that keeps money correct under retries, concurrency, and failures. It records money movements between accounts and guarantees they balance; it does not move real money.

> **Status:** early development (Milestone 1 of 7: Foundation). Most of what's described below is planned, not yet built. See [docs/design.md](docs/design.md) for the full scope, API contract, and milestone plan.

## What it does

- Accounts and double-entry transactions: every transfer writes entries that sum to zero per currency
- Idempotent transfer API: retrying a request with the same `Idempotency-Key` never moves money twice
- Cached balances, verified against immutable, append-only entries
- Reversals as new compensating entries, never edits or deletes
- Concurrency safety through row locks taken in a fixed order, and a reconciliation job that detects balance drift

Out of scope: user login, any UI, real payment networks, currency conversion, and production deployment.

## Stack

Python 3.13, FastAPI (synchronous handlers), PostgreSQL 18 accessed with raw SQL (no ORM) through `psycopg` and `psycopg_pool`, plain numbered SQL migration files, `uv` for dependencies, `ruff` for linting, pytest with Hypothesis, Locust for load tests, Docker Compose, GitHub Actions.

Amounts are stored as integers in minor units (for example, cents) with a currency code, never as floats.

## Running locally

```bash
docker compose up --build
```

This starts PostgreSQL 18 and the service on http://localhost:8000. The first start of an empty database creates two roles (`docker/roles/roles.sql`): `ledger_owner`, which owns the schema and runs the migrations, and `ledger_app`, which the service connects as and which owns nothing. A one-shot `migrate` service applies pending migrations from `migrations/` as the owner role and exits; the `app` service starts only after it succeeds and receives only the app role's credentials. The passwords in `compose.yaml` are development-only defaults; override them with the `POSTGRES_PASSWORD`, `LEDGER_OWNER_PASSWORD`, and `LEDGER_APP_PASSWORD` environment variables.

To apply migrations by hand against the local database: `PYTHONPATH=src uv run python -m ledger.migrate`.

## Running tests

Tests run against a real PostgreSQL, never mocks. Start the database, then run pytest:

```bash
docker compose up -d --wait db
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

CI runs `uv run pytest --cov`, which also measures branch coverage and fails below the floor set in `pyproject.toml`; run the same command locally to see which lines are missed.

The defaults point at the Compose database on `127.0.0.1:5432`. Set `DATABASE_OWNER_URL` and `DATABASE_APP_URL` to use another one. Test fixtures connect as the owner role to migrate, reset, and seed data, and as the app role for everything under test.

## API

| Endpoint | Purpose | Planned in |
| --- | --- | --- |
| `POST /accounts` | Create an account (`name` and `currency`; names are unique) | Milestone 3 |
| `GET /accounts/{id}/balance` | Cached balance; reflects committed transfers and never shows part of one | Milestone 3 |
| `POST /transfers` | Move money between two accounts (requires `Idempotency-Key`) | Milestone 3 |
| `GET /transfers/{id}` | Fetch one transfer with its entries | Milestone 3 |
| `GET /accounts/{id}/entries` | Entry history, newest first, with cursor pagination | Milestone 3 |
| `POST /transfers/{id}/reverse` | Undo a transfer with a compensating transaction (requires `Idempotency-Key`) | Milestone 5 |

Request and response shapes, error codes, and idempotency rules are specified in [docs/design.md](docs/design.md).

## Correctness guarantees

These invariants will be defended by property-based, concurrency, and failure-injection tests:

1. Entries in each transaction sum to zero per currency.
2. Entries are never changed after they are written, and the database itself refuses UPDATE and DELETE on them.
3. The cached balance always equals the sum of that account's entries.
4. An account that doesn't allow negative balances never goes below zero, enforced in the transfer code and by a database CHECK on cached balances.
5. The same idempotency key never moves money twice.
6. Every entry's currency matches its account's currency.

## Design decisions

_TODO: integer minor units, isolation level, and idempotency design (Milestone 7)._

## Benchmarks

_TODO: load-test results with the command and hardware spec that produced them (Milestone 7)._

## Known limits

Known by design so far; more will be added in Milestone 7.

- There is no authentication or authorization, so anyone who knows a funding account's id can transfer out of it with `POST /transfers` and effectively mint money. The API cannot create such accounts; only the seed script can. That is acceptable for this scope, but a real deployment would need authentication and per-account authorization first.
- Idempotency keys are stored for the life of the data and never expire.
- Creating an account is not idempotent: retrying after a lost response returns 409 for the duplicate name, and there is no lookup endpoint.
- The balance endpoint reads a cache, so it may not include a transfer still in flight.
- A single transfer is capped at 99,900,000,000 minor units, regardless of the currency's minor-unit size.
- The rule "no leading or trailing whitespace" for account names is only defined for ASCII whitespace (space, tab, newline). Whether Unicode whitespace such as a non-breaking space counts is not yet decided, and the API check and the database CHECK could disagree on it until it is. To be settled before milestone 1 ends.
