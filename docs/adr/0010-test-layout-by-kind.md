# ADR 0010: Tests are laid out by kind, and only the database tests need a database

* **Status:** Accepted
* **Date:** 2026-10-12
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
Every test lived in one flat `tests/` folder, 25 files and 339 tests by the end of milestone 3's first PR. Classifying them showed they sort cleanly: 107 unit tests with no database, about 230 integration tests that use one, and a Hypothesis state machine. Only two files mixed kinds. A session-wide autouse fixture applied the migrations before every test, so even the pure unit tests could not run without a running Postgres, and a missing database showed up as errors in tests unrelated to it. ADR 0003 describes the real-database approach this refines.

## Decision
- **Folders by kind, with areas inside:**
  - `tests/unit/` (with `unit/api/`): no database, no app lifespan.
  - `tests/integration/` with `db/` (schema, privileges, roles, triggers, the migration runner), `service/` (accounts and posting) and `api/` (the real app).
  - `tests/property/`: Hypothesis tests, both database-backed ones and pure ones.
  - `tests/e2e/` (added by ADR 0013): tests that cross layers on purpose, through the real app or a real server: the commit-time safety net, concurrent requests, the real startup. `tests/integration/api/` is then only the HTTP contract.
  - A `concurrency/` folder joins them in milestone 4.
  - *Note (2026-10-16, #61): "tests that cross layers on purpose" did not separate `e2e` from `integration/api`, and it is replaced by two definitions. `tests/e2e/` is black-box: a live server process, with every action and assertion going over the network, and no `TestClient`, direct database access, monkeypatching or look at internals such as `pg_locks` in the test body. The funding accounts are created beforehand by the seed script, run as a separate process (#62). `tests/integration/api/` is the HTTP contract through `TestClient`, and may use mocks, injected failures and held locks for determinism. The existing `tests/e2e/` files are audited against these in small follow-up PRs under #61; until each is moved, the folder still holds tests that do not meet the definition.*
- **Fixtures are not autouse.** `migrated_database` is a normal session fixture that `owner_conn` and `app_pool` depend on, so a test that uses the database gets migrated tables and a test that does not never connects. The property test and the app tests request it explicitly with `usefixtures`.
- **`reset_database` lives in `tests/db_helpers.py`,** not in `conftest.py`, so tests no longer import from a conftest.
- **`test_migrate.py` is split** into `unit/test_migrate_files.py` (finding and ordering files) and `integration/db/test_migrate_runner.py` (applying them).
- **Files in an area lose their `test_api_` style prefix,** because the folder says it.
- **The rule is enforced, so CI fails if a unit test breaks it** (`tests/unit/conftest.py`, with the logic in `tests/db_guard.py`):
  - At collection, a unit test that depends on a database fixture (`migrated_database`, `owner_conn` or `app_pool`, directly or through a fixture built on them) stops the whole run with a usage error that lists the offenders.
  - While a unit test runs, an autouse fixture makes every way psycopg opens a connection raise.
  - The guard has its own tests, including a nested pytest run (pytest's `pytester`) that proves the hook and the fixture are wired in, not just that the helper function works.

## Consequences
* **Positive:**
  - `uv run pytest tests/unit` runs in about a second with no Docker, and a developer without a database can still check the pure logic.
  - A failing connection now fails the integration tests that need it, not unrelated ones.
  - A unit test that reaches for the database fails in CI, where a Postgres service would otherwise hide the mistake.
  - Each kind has an obvious home, and the folders match the kinds in the design doc's testing table.

* **Negative / Trade-offs:**
  - New tests need a decision about where they belong, and the boundary is a judgement for files like the real-app tests, which start the app against the database.
  - Property is a technique, not a level, so `property/` holds some tests that need a database and some that do not.
  - Moved files change paths in history and in a few docstrings.
  - The guard cannot see a connection made in another process (the logging test runs a subprocess, which never connects), or by a driver other than psycopg. A new fixture that opens its own connection must depend on one of the base database fixtures, or be added to `DATABASE_FIXTURES`.
  - Starting the app with the database stopped fails slowly (about two minutes, in the connection pool's wait), which this change does not address.

* **Alternatives Considered:**
  - **Folders by area (`db/`, `service/`, `api/`):** rejected, because it does not separate tests that need a database from those that do.
  - **Markers only, with no move:** rejected. It leaves 25 files in one folder and does nothing for the autouse fixture by itself.
  - **Markers on top of the folders** (`-m unit`): possible later; the folders already give `pytest tests/unit`.
  - **Only the collection-time check:** rejected, because a test could still open a connection itself. **Only the runtime block:** rejected, because a fixture-based mistake is clearer when it stops the run at collection.
  - *Note (2026-10-16, #61): for funding accounts in the black-box e2e tests, reserved negative account ids for clearing-house accounts were rejected. The API's id bounds (1 to the bigint maximum) would make a transfer from a negative id a 400, `accounts_id_positive` (migration 008) would have to be replaced, the currency set is open so a migration cannot list one account per currency, and it needs a schema change and a public API change. Exposing `allow_negative` in `POST /accounts` was ruled out by the design doc. The chosen approach is a seed script run as a separate process (#62).*
