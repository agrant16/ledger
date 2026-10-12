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
  - A `concurrency/` folder joins them in milestone 4.
- **Fixtures are not autouse.** `migrated_database` is a normal session fixture that `owner_conn` and `app_pool` depend on, so a test that uses the database gets migrated tables and a test that does not never connects. The property test and the app tests request it explicitly with `usefixtures`.
- **`reset_database` lives in `tests/db_helpers.py`,** not in `conftest.py`, so tests no longer import from a conftest.
- **`test_migrate.py` is split** into `unit/test_migrate_files.py` (finding and ordering files) and `integration/db/test_migrate_runner.py` (applying them).
- **Files in an area lose their `test_api_` style prefix,** because the folder says it.

## Consequences
* **Positive:**
  - `uv run pytest tests/unit` runs in about a second with no Docker, and a developer without a database can still check the pure logic.
  - A failing connection now fails the integration tests that need it, not unrelated ones.
  - Each kind has an obvious home, and the folders match the kinds in the design doc's testing table.

* **Negative / Trade-offs:**
  - New tests need a decision about where they belong, and the boundary is a judgement for files like the real-app tests, which start the app against the database.
  - Property is a technique, not a level, so `property/` holds some tests that need a database and some that do not.
  - Moved files change paths in history and in a few docstrings.
  - Starting the app with the database stopped fails slowly (about two minutes, in the connection pool's wait), which this change does not address.

* **Alternatives Considered:**
  - **Folders by area (`db/`, `service/`, `api/`):** rejected, because it does not separate tests that need a database from those that do.
  - **Markers only, with no move:** rejected. It leaves 25 files in one folder and does nothing for the autouse fixture by itself.
  - **Markers on top of the folders** (`-m unit`): possible later; the folders already give `pytest tests/unit`.
