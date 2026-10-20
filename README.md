# Ledger Service

A small double-entry ledger service that keeps money correct under retries, concurrency, and failures. It records money movements between accounts and guarantees they balance; it does not move real money.

> **Status:** early development. Milestones 1 (foundation: schema for accounts, transactions and entries, role setup, CI), 2 (the double-entry core) and 3 (the API and idempotency) are complete: account creation, the balance endpoint, `GET /transfers/{id}`, idempotent `POST /transfers`, the entries history, the overload handling (pool timeout, thread limit, concurrency cap) and the OpenAPI documentation of every route's error responses. Milestone 4 (concurrency) is next. The rest of what's described below is still planned. See [docs/design.md](docs/design.md) for the full scope, API contract, and milestone plan.

## What it does

- Accounts and double-entry transactions: every transfer writes entries that sum to zero per currency
- Idempotent transfer API: retrying a request with the same `Idempotency-Key` never moves money twice
- Cached balances, verified against immutable, append-only entries
- Reversals as new compensating entries, never edits or deletes
- Concurrency safety through row locks taken in a fixed order, and a reconciliation job that detects balance drift

Out of scope: user login, any UI, real payment networks, currency conversion, and production deployment.

## Stack

Python 3.13, FastAPI (synchronous handlers), PostgreSQL 18 accessed with raw SQL (no ORM) through `psycopg` and `psycopg_pool`, plain numbered SQL migration files, `uv` for dependencies, `ruff` for Python linting, SQLFluff for the SQL migrations, pytest with Hypothesis, Locust for load tests, Docker Compose, GitHub Actions.

Amounts are stored as integers in minor units (for example, cents) with a currency code, never as floats.

## Running locally

```bash
docker compose up --build
```

This starts PostgreSQL 18 and the service on http://localhost:8000. The first start of an empty database creates two roles (`docker/roles/roles.sql`): `ledger_owner`, which owns the schema and runs the migrations, and `ledger_app`, which the service connects as and which owns nothing. A one-shot `migrate` service applies pending migrations from `migrations/` as the owner role and exits; the `app` service starts only after it succeeds and receives only the app role's credentials. The passwords in `compose.yaml` are development-only defaults; override them with the `POSTGRES_PASSWORD`, `LEDGER_OWNER_PASSWORD`, and `LEDGER_APP_PASSWORD` environment variables.

To apply migrations by hand against the local database: `PYTHONPATH=src uv run python -m ledger.migrate`.

To start over with an empty local database, run `PYTHONPATH=src uv run python scripts/reset_db.py` (add `--yes` to skip the confirmation). It deletes all data in the database named by `DATABASE_OWNER_URL`, restarts the identity counters at 1 and re-applies every migration. It refuses any host that is not this machine. The test suite uses its own `ledger_test` database, so it is unaffected.

To fill the local database with accounts for manual API testing, run `PYTHONPATH=src uv run python scripts/seed_dev.py`. It creates a funding account (`allow_negative`) and five protected customer accounts for each of USD and EUR through `create_account`, all with a balance of 0, and skips any name that already exists, so it is safe to run on a database that holds data you created through the API. `reset_db.py --seed` does both in one command, and the ids are then always the same (funding accounts 1 and 2, USD customers 3 to 7, EUR customers 8 to 12).

### Configuration

The service and its tools read these environment variables (`Settings` in `src/ledger/infra/config.py`). Every one has a default, so none is required for local development.

| Variable | Default | Used by | What it does |
| --- | --- | --- | --- |
| `DATABASE_APP_URL` | `postgresql://ledger_app:app-dev-only@127.0.0.1:5432/ledger` | the service, `ledger.seed`, the dev scripts | Connection string for the app role, which owns no tables. The service never connects as the owner. |
| `DATABASE_OWNER_URL` | `postgresql://ledger_owner:owner-dev-only@127.0.0.1:5432/ledger` | `ledger.migrate`, `scripts/reset_db.py`, test fixtures | Connection string for the owner role, used only to run migrations and reset data. |
| `MIGRATIONS_DIR` | the repository's `migrations/` folder | `ledger.migrate`, `scripts/reset_db.py` | Where the numbered SQL migration files are read from. |
| `POOL_SIZE` | `10` | the service | Number of connections in the fixed pool (a positive integer). |
| `POOL_TIMEOUT_SECONDS` | `2.0` | the service | How long a request waits for a pooled connection before it gets a 503 with `Retry-After` (a number greater than 0). |
| `HANDLER_THREAD_LIMIT` | `20` | the service | Maximum number of handler threads running at once (a positive integer). |
| `CONCURRENCY_CAP` | `40` | the service | Maximum number of requests in flight before a 503 with `Retry-After` (a positive integer). |

A value that does not validate stops the service at startup with an error that names the variable. The pool size, pool wait, thread limit and concurrency cap are logged in one INFO line when the service starts, so the effective values are on record; the database URLs are not.

Docker Compose builds the two URLs for its containers from three more variables that only Compose reads, and no Python code does: `POSTGRES_PASSWORD` (the superuser, used by the database container), `LEDGER_OWNER_PASSWORD` (default `owner-dev-only`) and `LEDGER_APP_PASSWORD` (default `app-dev-only`). These defaults are for development only.

### Funding accounts (the seed script)

There is no deposit endpoint: money enters the ledger through transfers out of a funding account, an account that is allowed to go negative. The API cannot create one, so `ledger.seed` does, as the app role and through the service's `create_account`:

```bash
PYTHONPATH=src uv run python -m ledger.seed --output seed_ids.json
```

```text
created funding:USD (id 1)
created funding:EUR (id 2)
seeded 2 funding accounts, ids written to seed_ids.json
```

| Option | Meaning |
| --- | --- |
| `--output PATH` (`-o`) | Required. Where the ids file is written, atomically after the accounts are committed. The file is not ignored by git, so keep it out of commits. |
| `--currency CODE` | Three uppercase letters; repeat it for more. Naming any currency replaces the defaults, which are `USD` and `EUR`. A repeated currency is used once. |
| `--per-currency N` | Funding accounts per currency, 1 to 500 (default 1). A larger value is refused, not cut down. |

The first account of a currency is named `funding:<CURRENCY>` and further ones `funding:<CURRENCY>:2`, `:3` and so on. The ids file has this shape, each currency mapping to its account ids in that order:

```json
{ "funding_accounts": { "USD": [1], "EUR": [2] } }
```

The script is safe to run again, and two runs at once are safe too: an account that already exists is reused and its id reported (`exists funding:USD (id 1)`). An existing account with the same name but a different currency, or one that cannot go negative, fails the run instead: the script exits 1, names the account, writes no file and rolls back everything that run created. Exit codes: 0 success, 1 failure, 2 usage error. It reads `DATABASE_APP_URL` like the service and never uses the owner role, so each funding account goes negative by exactly what it has issued and the balances of a currency still sum to zero.

## Running tests

Tests run against a real PostgreSQL, never mocks. Start the database, then run pytest:

```bash
docker compose up -d --wait db
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run sqlfluff lint migrations/
```

The tests are laid out by kind: `tests/unit` needs no database (`uv run pytest tests/unit`), `tests/integration`, `tests/property` and `tests/e2e` do. `tests/integration/api` checks the HTTP contract through `TestClient` and may use mocks, injected failures and held locks. `tests/e2e` is black-box: it runs against a live server process, and every action and assertion goes over the network, with no `TestClient`, direct database access or monkeypatching in the test body. Its funding accounts come from the seed script, run as a separate process. See ADRs 0010 and 0013.

CI runs `uv run pytest --cov`, which also measures branch coverage and fails below the floor set in `pyproject.toml`; run the same command locally to see which lines are missed.

The defaults point at the Compose server on `127.0.0.1:5432`. Set `DATABASE_OWNER_URL` and `DATABASE_APP_URL` to use another one.

The tests run in their own database, `ledger_test`, on the same server and with the same roles. Every test run empties its tables, so it must not use the `ledger` database that the running service uses: whatever you created through the API stays untouched. A fresh Compose volume creates `ledger_test` along with `ledger` (`docker/roles/roles.sql`). A volume created before this was added needs it once:

```bash
docker compose exec -T db psql -U postgres -d ledger -v ON_ERROR_STOP=1 -v testdbname=ledger_test -f /docker-roles/test-database.sql
```

(Or start over with `docker compose down -v`, which deletes the service's data too.) Test fixtures connect as the owner role to migrate, reset, and seed data, and as the app role for everything under test.

## API

| Endpoint | Purpose | Status |
| --- | --- | --- |
| `POST /accounts` | Create an account (`account_name` and `currency`; names are unique) | Done |
| `GET /accounts/{id}/balance` | Cached balance; reflects committed transfers and never shows part of one | Done |
| `POST /transfers` | Move money between two accounts (requires `Idempotency-Key`) | Done |
| `GET /transfers/{id}` | Fetch one transfer with its entries | Done |
| `GET /accounts/{id}/entries` | Entry history, newest first, with cursor pagination | Done |
| `POST /transfers/{id}/reverse` | Undo a transfer with a compensating transaction (requires `Idempotency-Key`) | Planned in milestone 5 |

Request and response shapes, error codes, and idempotency rules are specified in [docs/design.md](docs/design.md).

## Correctness guarantees

These invariants will be defended by property-based, concurrency, and failure-injection tests:

1. Entries in each transaction sum to zero per currency.
2. Entries are never changed after they are written, and the database itself refuses UPDATE and DELETE on them for the service's role (the owner role, used only for migrations and test fixtures, is not restricted).
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
- Error responses include the data an error carries (for example the account id, balance and amount for insufficient funds). With no authentication that exposes nothing new here, but a production service would trim them.
- Creating an account is not idempotent: retrying after a lost response returns 409 for the duplicate name, and there is no lookup endpoint.
- The balance endpoint reads a cache, so it may not include a transfer still in flight.
- A single transfer is capped at 99,900,000,000 minor units, regardless of the currency's minor-unit size.
- Account names reject only ASCII whitespace (space, tab, newline, vertical tab, form feed, carriage return) at either end. A name that starts or ends with a Unicode space such as a non-breaking space is accepted as a different, visually similar name, as with names that differ only by case.
- `created_at` columns are filled by a database default (`now()`), but the app role has plain INSERT on those tables and could supply its own value. The service never does, and no invariant depends on it. Enforcing it would take a column-level INSERT grant or a trigger.
