# ADR 0013: A layered source tree: API, services, repositories, models and infrastructure

* **Status:** Draft
* **Date:** 2026-10-13 (decided before milestone 3 PR 5)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
`src/ledger` grew one milestone at a time as a flat set of modules. After milestone 3's first four pull requests it has four problems. Infrastructure (`db.py`, `config.py`, `logging_config.py`) and business logic (`accounts.py`, `posting.py`) sit at the same level. The business modules also hold the SQL, so the rules and the statements that carry them out are read together and cannot be reviewed apart. The same file names exist at two levels (`accounts.py` and `api/accounts.py`, and `transfers.py` is next), and `logging_config.py` imports from `api/request_id.py`, which points the dependency the wrong way. A routes file also holds its request models, its response models and its handlers. The remaining milestone 3 work (`GET /transfers/{id}`, `POST /transfers`, entries pagination) is SQL-heavy, so every pull request written in the old shape would have to be moved later. The structure should also leave room for a second delivery mechanism (gRPC or GraphQL), which is a stretch goal.

## Decision
- **Five layers with one-way imports.** `errors` and `models` import nothing from `ledger`. `infra` imports only `errors`. `repository` imports `models` and `errors`. `service` imports `repository`, `models` and `errors`. `api` imports `service`, `models`, `errors` and `infra`. `main.py` and `migrate.py` are entry points: they may import any layer, and nothing imports them. A test reads the source with the `ast` module and enforces this, including that services never import the pool, `psycopg.errors` or `ledger.infra`, and that the API layer never imports `psycopg` or the pool directly.
- **The layout** (suffixes keep names unique across layers):

  | Package | Holds |
  | --- | --- |
  | `ledger/errors.py` | The error taxonomy (ADR 0007). |
  | `ledger/models/` | Domain dataclasses by area: `account_model.py`, `posting_model.py`, `transfer_model.py`. Used by the repositories, the services and the API's response schemas. |
  | `ledger/repository/` | Plain functions that run parameterized SQL on the connection they are given and return models: `account_repo.py`, `posting_repo.py`. No classes. |
  | `ledger/service/` | The business rules: `account_service.py`, `posting_service.py`, `transfer_service.py`, and `decorators.py`. A service function takes the request's connection and never commits. |
  | `ledger/infra/` | `db.py` (`create_pool`, `run_in_transaction`), `config.py`, `logging_config.py` (log format, request-id variable and record factory, the uvicorn duplicate filter). |
  | `ledger/api/rest/` | `router.py` (one aggregate router for `main.py`), `routes/`, `schemas/` (request and response models and the shared validated types), `middleware/`, the RFC 9457 error handling, and the idempotency request hash. A later `api/grpc/` or `api/graphql/` would sit beside it. |

- **The API layer is the transaction boundary,** as ADR 0011 says. A route calls `run_in_transaction(pool, lambda conn: some_service_function(conn, ...))` once, and the service layer is always handed an open connection. ADR 0011 is unchanged.
- **Services check the connection with a decorator,** `@transactional_service_method`, which refuses a connection in autocommit mode with a `LedgerInvariantError` naming the function. It replaces the `require_transaction(conn, name)` call at the top of each function.
- **SQL lives only in the repository layer.** The rules stay in the service. For example, the claim becomes two repository functions ("insert the claim if the key is free" and "look up the key") and a service function that decides between a replay and a conflict. `post_transaction`'s checks stay in the service, and the locking `SELECT ... FOR UPDATE`, the entry insert and the balance update become repository functions whose contracts are unchanged (ADR 0006).
- **Database errors are translated, once, at the boundary of the layer that owns them.** This amends ADR 0007, which said CHECK errors "are not translated". The repository raises a `LedgerError` for an expected constraint outcome (a duplicate account name now, the reversal link in milestone 5), and a `LedgerInvariantError` chained to the original with `from` for any other `UniqueViolation` or `CheckViolation`. A constraint violation that happens at commit (the deferred triggers) cannot be seen by a repository, so `run_in_transaction` translates any `IntegrityError` the same way. A database safety net is still a bug, never a `LedgerError`: it still returns a logged 500 and is never retried. No raw psycopg integrity error crosses a layer boundary.
- **Tests follow the layers.** `tests/integration/db` covers the schema, the constraints and the repository functions. `tests/integration/service` covers the business rules against the real database. `tests/integration/api` verifies only the HTTP contract (still against the real database). Tests that deliberately cross layers (the commit-time safety-net matrix, concurrent same-key requests, the API-level state machine, the real-app tests) go in `tests/system`. `tests/unit` gains the import-direction test, which is the acceptance check for every step of the move.
- **The move is made in steps,** each leaving the whole suite green and each tightening the import-direction test: (1) infrastructure, models, the decorator and the import-direction test; (2) the repository layer and the services; (3) the API package, the test reclassification and `tests/system`. The URLs do not change.

## Consequences
* **Positive:**
  - The rules read without SQL in the way, and the SQL can be reviewed, tested and indexed in one place.
  - Dependencies run one way, enforced by a test, so the structure cannot erode quietly.
  - The names are unambiguous (`account_service`, `account_repo`, `account_schema`), and the future gRPC or GraphQL delivery has an obvious home.
  - A service function cannot take a second connection or commit, because it is handed one and the pool is out of its reach.
  - Every database error that reaches the API layer is already a `LedgerError` (expected) or a `LedgerInvariantError` (a bug).

* **Negative / Trade-offs:**
  - More files and one more hop to follow for a small codebase (about a thousand lines at the start).
  - A large move: most import paths and several patch-target strings in the tests change, and it has to be done in steps to stay reviewable.
  - Translating constraint errors at two places (repository and `run_in_transaction`) needs tests at both, and the earlier tests that expected a raw `CheckViolation` change.

* **Alternatives Considered:**
  - **Keep the flat layout and rename files:** rejected. It removes the duplicate names but leaves infrastructure mixed with business logic and SQL mixed with rules.
  - **Repository interfaces (abstract base classes or protocols) with in-memory fakes:** rejected. Tests run against the real database (ADR 0003), so there is nothing to swap in, and the abstraction would add code without a user.
  - **Services that own the transaction (taking the pool):** rejected. The transaction boundary belongs at the HTTP edge (ADR 0011), so a retry can wrap the whole request body and a commit failure surfaces inside the handler.
  - **Translating every `CheckViolation` into a `LedgerError`:** rejected. A safety net that fires is a bug and must be logged as one; as an expected error it would be silent.
  - **An ORM:** already rejected in ADR 0001.
