# ADR 0003: Tests against a real Postgres, written first, behind a CI merge gate

* **Status:** Accepted
* **Date:** 2026-10-08 (extended through 2026-10-09 with the CI hardening changes)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
The properties this project claims (no double spend, no overdraft, entries that always balance) depend on database behavior: row locks, constraints, deferred triggers, privileges. A mock cannot show any of that. The project also has a deliberate division of work: the author writes the core ledger logic, and Claude writes tests and scaffolding, so the process has to make that division work and keep `main` trustworthy.

## Decision
- **Tests run against a real PostgreSQL 18**, locally through Docker Compose and in CI as a service container. No mocks for the database. Since milestone 3 the tests run in their own `ledger_test` database on the same server, because every test run empties its tables and must not wipe the data of the service a developer is running.
- **Two connections in tests.** `owner_conn` resets and seeds data; `app_pool` and `app_conn` are the role the service uses and are the only ones under test. `app_conn` rolls back at teardown, which keeps ordinary tests clear of the commit-time triggers. A `commit_setup` fixture commits just what a second connection needs to see and truncates everything afterwards. Migration tests run in throwaway schemas.
- **Tests first for the author's core code.** For transfer logic, concurrency, idempotency, migrations, and reconciliation, Claude writes red tests from `docs/design.md` and the author writes the implementation. A test is not changed to fit the code without saying why.
- **Tests are checked against broken implementations.** A new test is run against an empty skeleton to prove it is not vacuous, and against deliberately broken versions of the code to prove it can fail.
- **CI is the merge gate.** Every change goes on its own branch and pull request and is squash-merged. A repository ruleset requires both `lint-and-test` (ruff, `ruff format --check`, SQLFluff, then pytest with an 85% coverage floor) and `compose-smoke` (builds the image and checks `/health`). Throwaway probe PRs with a deliberately failing test confirmed that the gate blocks a merge.
- **CI hardening:**
  - `permissions: contents: read`.
  - Actions pinned to full commit SHAs, kept current by weekly Dependabot PRs: GitHub Actions in one group, Python packages ungrouped, Docker and Compose images with major-version ignores.
  - A pinned runner image (`ubuntu-24.04`) and a 10-minute job timeout.
  - A concurrency rule that cancels superseded runs on pull requests only, so every push to `main` still runs.

## Consequences
* **Positive:**
  - Locking, constraint, and trigger behavior is exercised for real.
  - The division of work is visible in the history: red tests first, then the implementation.
  - A green `main` is enforced mechanically, not by habit.

* **Negative / Trade-offs:**
  - The suite needs a running database, and some tests are slower than pure unit tests.
  - Intermediate commits on a branch may be red, because tests land before the implementation.
  - Tests that truncate tables must not share the dev database with data worth keeping.
  - `pytest-timeout` is deferred to milestone 4, when tests start to wait on locks.

* **Alternatives Considered:**
  - **Mocking the database layer:** rejected, because it could not show lock, constraint, or trigger behavior.
  - **Classic branch protection rules:** a ruleset was used instead, as the newer mechanism.
  - **Floating action tags (`@v4`):** replaced by SHA pins and Dependabot.
