# Ledger Service

A double-entry ledger service I'm building as a portfolio project, working with Claude Code. The goal is a correct, well-tested system I can explain in depth, not just working code.

## Read first

The project scope, architecture, data model, API design, milestones, and testing plan are in `docs/design.md`. Read it before starting any work, and follow its milestone order. It is the source of truth for the design. I keep some planning notes elsewhere, so don't assume anything outside this repo matches it. If the repo layout or stack differs from the design doc, tell me instead of silently diverging. If we change a design decision, update the design doc in the same change.

## Stack and conventions

The stack below is decided and pre-approved. Don't substitute alternatives.

- Python 3.13, FastAPI with synchronous handlers, PostgreSQL 18, raw SQL via psycopg (no ORM), `psycopg_pool`, `uv` with `pyproject.toml`, `ruff`, pytest with Hypothesis, Locust, Docker Compose, GitHub Actions.
- Amounts are integers in minor units (cents) plus a currency code. Never floats.
- Every write is one database transaction. A request uses one connection for its whole transaction and never takes a second one while holding the first.
- Lock `balances` rows with `SELECT ... FOR UPDATE` in ascending account id, at READ COMMITTED. Check overdraft against the balance returned by that same locking statement.
- Migrations are numbered plain SQL files applied by a small runner (no Alembic), run as the owner role. The service connects as the app role, which owns no tables and has explicit per-table grants written in the migration that creates each table. Never connect as the superuser or owner, and never use `ALTER DEFAULT PRIVILEGES`.
- Ids are `bigint GENERATED ALWAYS AS IDENTITY`.
- Validation errors return 400, not FastAPI's default 422. 422 means insufficient funds.
- Tests run against a real Postgres, not mocks.
- Service functions take the request's connection and never commit; the caller owns the database transaction, so a retry wraps the key claim and the posting together.
- Expected failures are `LedgerError` subclasses in `ledger/errors.py`, and the API layer maps each to an HTTP status. Bugs (`LedgerInvariantError`, database safety-net violations) are not `LedgerError`s and return a logged 500.

## How we split the work

This project has to demonstrate my own skills, so the division matters.

**I write myself:**
- Transfer logic and the core ledger invariants (`post_transaction` and the code around it)
- Concurrency handling: lock ordering, retries, timeouts
- Idempotency: the key claim, request hashing, and replay behavior
- The data model and migrations for the core tables, including their grants and constraints
- The reconciliation job
- The API layer: routes, request validation and error mapping, and pagination

**Claude can do:**
- Project scaffolding, config, and boilerplate (Docker Compose, CI, the app-role init script, the migration runner)
- OpenAPI docs and descriptions for the endpoints I write (including the balance endpoint's cached-read note)
- Test generation and test infrastructure, including Hypothesis and concurrency harnesses
- The Locust load test and benchmark scripts
- Code review of what I write: bugs, edge cases, missed invariants
- Docs, README drafts, and tooling scripts

If I ask Claude to write something from my "I write myself" list, ask me to confirm first. If I say yes, walk me through the code afterward so I can explain it. If a task falls between the two lists, ask me which side it belongs on rather than guessing.

## Correctness rules

These properties must always hold. Tests should check them directly, preferably as property-based tests and concurrency tests, not only example-based ones.

- Every transaction's entries sum to zero per currency (debits equal credits).
- Replaying the same transfer request with the same idempotency key never applies it twice.
- No account balance goes negative under concurrent transfers, except accounts created with `allow_negative` true (the funding accounts).
- Ledger entries are append-only, and the database itself refuses UPDATE and DELETE on them. Corrections are made with new entries, never by editing or deleting old ones.
- The cached balance always equals the sum of that account's entries.
- Every entry's currency matches its account's currency.

These mirror the invariants list in `docs/design.md`. If the two ever disagree, tell me and we'll fix both.

Write tests against these properties, not against whatever the current code happens to do. If a test fails, don't change the test to match the code without telling me why.

## Working style

- Work one milestone at a time, and keep each change small enough for me to review. A milestone's "Done when" line is its acceptance check.
- Make small, focused commits with clear messages. Don't bundle unrelated changes.
- Before writing code for a non-trivial feature, briefly state the approach and any tradeoffs so I can weigh in.
- Explain any non-obvious design choice in plain terms, so I can explain and defend my decisions.
- Dependencies beyond the stack above, schema changes, and changes to the public API need my approval first. Adding the pre-approved stack's own packages during scaffolding is fine.
- Every change goes on its own branch and a pull request, never straight to `main`. Both CI checks (`lint-and-test` and `compose-smoke`) must pass, and PRs are squash-merged, so intermediate commits on a branch may be red.
- For the items on my "I write myself" list, Claude writes the tests first from `docs/design.md` and I write the implementation; the tests stay red until it exists.

## Commands

These were verified in milestone 1.

- Start the database the tests need: `docker compose up -d --wait db`
- Run tests: `uv run pytest` (CI runs `uv run pytest --cov`, which also enforces the coverage floor)
- Run linter: `uv run ruff check .`
- Run formatter: `uv run ruff format .`
- Run the service locally: `docker compose up --build` (a one-shot `migrate` service applies migrations first)
- Apply migrations by hand: `PYTHONPATH=src uv run python -m ledger.migrate`
