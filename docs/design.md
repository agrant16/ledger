# Ledger Service Design

## Purpose

A small double-entry ledger service that shows how to keep money correct under retries, concurrency, and failures: idempotent transfers, row-level locking, append-only entries, and reconciliation. It is a portfolio project, so there is no UI, no real money, and no users.

## Scope and stack

The service records money movements between accounts and guarantees they balance; it does not move real money.

| In scope | Out of scope |
| --- | --- |
| Accounts and double-entry transactions | User login and permissions |
| Idempotent transfer API | Any UI |
| Cached balances verified against immutable entries | Real payment networks or cards |
| Reversals as new compensating entries | Currency conversion (stretch goal) |
| Concurrency safety and reconciliation | Multi-region or sharding (discuss only) |
| Tests, CI, load test, README | Production deployment |

**Stack:** Python 3.13 with FastAPI (synchronous handlers), PostgreSQL 18 accessed with raw SQL through `psycopg` and `psycopg_pool`, `uv`, `ruff` and SQLFluff, pytest with Hypothesis, Locust, Docker Compose, GitHub Actions.

**Stack decisions:**

- **Language:** Python 3.13 (decided; Java with Spring Boot was the considered alternative). The version is pinned by `requires-python` in `pyproject.toml` and by the Dockerfile's base image.
- **Database access:** raw SQL, no ORM, so locking and isolation levels are explicit and easy to explain.
- **Concurrency model:** synchronous handlers. Concurrency tests still fire parallel requests with threads.
- **Connection pool:** `psycopg_pool`, a fixed pool of 10 connections, with the handler thread pool set to 20 at startup (FastAPI's default is 40). The extra threads wait on the pool with a timeout, so a request that cannot get a connection becomes a bounded 503. The thread limit alone does not bound queueing, because requests beyond 20 in flight wait for a handler thread with no timeout, so the service also has an overall concurrency cap that rejects requests above it with a 503 (uvicorn's `--limit-concurrency`, or a small middleware). Both sizes come from environment variables, and the values used are recorded with every benchmark. A request uses one connection for its whole transaction and never takes a second one while holding the first, since a full pool of requests each waiting for a second connection would deadlock the service.
- **Request transactions:** a route calls `run_in_transaction(pool, work)` once (`ledger/infra/db.py`). It runs `work(conn)`, which does all of the request's database work, on one pooled connection as one database transaction and commits inside the handler, so a failure at commit, where the deferred triggers fire, is still a logged 500. A violated database constraint that reaches it is translated into a `LedgerInvariantError` chained to the psycopg error, so no raw integrity error crosses a layer (ADR 0013). `work` is a function so milestone 4's deadlock retry can run the whole body again. Reads pass `read_only=True`, which starts the transaction with `SET TRANSACTION READ ONLY`. See ADR 0011.
- **Migrations:** numbered plain SQL files, applied by a small runner (no Alembic). Once a migration is merged it is never edited except for formatting and comments; a schema change is a new migration (migration 005, for example, renames `accounts.name` to `accounts.account_name`).
- **Dependencies:** `uv` with `pyproject.toml`.
- **Linter/formatter:** `ruff` for Python, and SQLFluff (PostgreSQL dialect) for the migrations. SQLFluff's keyword rule means a SQL identifier must not be a keyword, which is why the account name column is `account_name` and not `name`.
- **Database:** PostgreSQL 18, pinned in Docker Compose and CI.
- **Load testing:** Locust (Python, so no extra language).

**Conventions:** amounts are integers in minor units (cents) with a currency code, never floats; every write goes through one database transaction; ids on accounts, transactions, and entries are `bigint GENERATED ALWAYS AS IDENTITY`, so the database assigns every id and the app role needs no sequence grants.

**Seed money:** there is no deposit endpoint. Money enters through transfers out of one funding account per currency, created by a seed script or test fixture through the service's `create_account` function with `allow_negative` true. The API does not expose that flag. A funding account goes negative by exactly what it has issued, so the sum of all balances in each currency is always zero.

## Architecture and data model

![Ledger architecture: 4 components, 4 tables](ledger-architecture.svg)

A transfer enters through the API; the ledger service inserts the transaction row (claiming the idempotency key), locks every touched balances row in account-id order, and writes the entries and balance updates in one commit, and the reconciliation job later checks those same tables.

**Terminology**

A *transaction* is a ledger record: one row in `transactions` plus its entries, which sum to zero per currency. A *database transaction* is the PostgreSQL one, and this document always says "database transaction" for that. A *transfer* is the API resource that creates a transaction, whether a normal transfer or a reversal, so `GET /transfers/{id}` returns a transaction and its entries, and `reverses_transaction_id` points at another transaction.

**Tables**

| Table | Key columns | Rule |
| --- | --- | --- |
| accounts | id, account\_name (unique), currency, allow\_negative, created\_at | `created_at` is `timestamptz NOT NULL DEFAULT now()`, set by the database default when the account is created. The name column is `account_name`: migration 005 renamed it from `name`, a keyword SQLFluff flags. The API field uses the same name, `account_name`. Currency never changes after creation: the app role has only INSERT and SELECT on accounts, so currency and allow\_negative are fixed for life. A unique (id, currency) backs the composite foreign key from entries, and a unique (id, allow\_negative) backs the one from balances |
| transactions | id, idempotency\_key (unique), request\_hash, reverses\_transaction\_id (nullable, unique), created\_at | One row per transfer or reversal; a reversal points at the original through a foreign key to transactions, so it cannot point at a transaction that does not exist, and the unique link enforces one reversal per transfer. Insert only: the app role has only INSERT and SELECT, so request\_hash and reverses\_transaction\_id cannot change after the fact. A CHECK requires request\_hash to be 64 lowercase hex digits (see Request hash) |
| entries | id, transaction\_id, account\_id, amount\_minor (signed), currency | Insert only; never updated or deleted (UPDATE, DELETE, and TRUNCATE are not granted to the app role, which owns no tables). Currency must match the account's, enforced by a composite foreign key, and transaction\_id references transactions (indexed, since every transaction lookup filters on it). A CHECK requires amount\_minor to be nonzero |
| balances | account\_id, allow\_negative, balance\_minor | A cache; entries are the source of truth. account\_id is the primary key, and (account\_id, allow\_negative) is a composite foreign key to accounts, so a balance row cannot exist for a missing account and its copy of allow\_negative always matches the account's (accounts are immutable to the app role). A CHECK (allow\_negative OR balance\_minor >= 0) makes a negative balance on a protected account impossible. The app role has SELECT, INSERT, and UPDATE but no DELETE or TRUNCATE |

**Invariants the tests defend**

1. Entries in each transaction sum to zero per currency, enforced in post\_transaction and, from milestone 2, by a deferred constraint trigger.
2. Entries are never changed after they are written, and the database itself refuses UPDATE and DELETE on them for the service's role (the owner role, used only for migrations and test fixtures, is not restricted).
3. The cached balance always equals the sum of that account's entries.
4. An account with `allow_negative` false never goes below zero, enforced in the transfer code at the lock and, from milestone 2, by a CHECK on balances.
5. The same idempotency key never moves money twice.
6. Every entry's currency matches its account's currency.

## API

Six endpoints cover the whole service; the transfer endpoint carries the interesting rules.

| Endpoint | Purpose | Key rules |
| --- | --- | --- |
| POST /accounts | Create an account | Request body is `account_name` and `currency` only (see Account request and response below); a duplicate name returns 409. Every account created through the API has `allow_negative` false: sending the field is rejected as an unknown field (400), so the API cannot create an account that may go negative. Its balances row is created in the same transaction, so there is always a row to lock; currency and `allow_negative` cannot change afterwards |
| GET /accounts/{id}/balance | Current balance | Response is `{ "account_id", "currency", "balance_minor" }`; 404 for an unknown account and 400 for a non-integer id. Reads the cached balance with a plain, lock-free read in a single statement joining balances to accounts. It reflects every transfer committed when the read ran, so it may not include one still in flight, and it never shows part of a transfer, because a transfer's entries and balance update commit together. The reconciliation job verifies the cache against the entries. The response carries no flag saying it is the cached value; the OpenAPI description and the README say so instead |
| POST /transfers | Move money between two accounts | Requires an `Idempotency-Key` header; returns the same response on replay (see Transfer response and Request hash below) |
| GET /transfers/{id} | Fetch one transfer with its entries | 404 if unknown |
| GET /accounts/{id}/entries | Entry history, newest first | Cursor pagination on `entries.id`, not offset. This is safe because every writer locks the account's balances row before inserting entries, so ids for one account increase in commit order and a page boundary never skips a row that commits late. Query parameters: `limit` (default 50, 1 to 200; anything outside that range is a 400, not clamped) and an optional `cursor`, the base64url of the last returned entry id, opaque to clients and unsigned; a cursor that does not decode to an id within the id bounds is a 400. Response is `{ "entries": [...], "next_cursor": "..." }` with `next_cursor` null on the last page and no total count. Each entry has the transfer response's entry shape plus `transaction_id` and `created_at`, the owning transaction's timestamp, read by joining `transactions` on its primary key. Entries are ordered by entry id, never by `created_at`: that is the transaction's start time, and a transfer that started earlier can wait for a lock and commit later, so timestamps are informational and not strictly in id order. The service fetches `limit + 1` rows to learn whether another page exists and returns only `limit`. An unknown account is a 404; a real account with no entries returns an empty page |
| POST /transfers/{id}/reverse | Undo a transfer | Requires an Idempotency-Key header; creates a new compensating transaction that mirrors each original entry with the amount negated (full reversals only); one reversal per transfer, and a reversal cannot itself be reversed |

**Id bounds**

Every id the API accepts, whether in a request body, a path, or a decoded cursor, must be an integer from 1 to 9,223,372,036,854,775,807, the `bigint` maximum. Anything outside that range, or not an integer, is rejected with a 400 before it reaches the database, so an oversized id never turns into a database error and a 500. A well-formed id that does not exist is still a 404. In a JSON body an id must be a JSON integer, never a string or a float such as `12.0`. In a URL it must be written as plain decimal digits, so a sign, spaces, underscores, a decimal point or a leading zero (`+12`, ` 12`, `1_000`, `12.0`, `012`) is a 400, not a different way of writing 12.

The database enforces the lower bound for the ids it generates. The `id` columns of `accounts`, `transactions` and `entries` are `GENERATED ALWAYS AS IDENTITY`, whose sequences start at 1, count up by 1 and never cycle, so they cannot produce a zero or a negative number. That alone is not a rule, because any role that may insert can add `OVERRIDING SYSTEM VALUE` and choose its own id. So each of the three columns also has a CHECK that `id > 0` (`accounts_id_positive`, `transactions_id_positive`, `entries_id_positive`, migration 008). Foreign key columns need no check of their own, since they can only point at rows that exist.

**Account request and response (`POST /accounts`)**

```json
{ "account_name": "customer:alice", "currency": "USD" }
```

- **`account_name`:** a string of 1 to 100 characters with no leading or trailing whitespace (rejected, not stripped). Whitespace here means the six ASCII whitespace characters only: space, tab, newline, vertical tab, form feed, and carriage return. Unicode spaces such as a non-breaking space are ordinary characters and are allowed. The API checks this with an explicit character set, not Python's `str.strip()`, and the database repeats the same rule in a CHECK so the two cannot disagree. A name that contains a NUL character or a lone surrogate is rejected too, because Postgres cannot store either. Names are unique, exact and case-sensitive, enforced by a unique constraint on `accounts.account_name`; a duplicate returns 409. The database enforces it, so two concurrent creates cannot both succeed.
- **`currency`:** the same rule as transfers: three uppercase letters, lowercase rejected.
- **Strict types and unknown fields:** the same rules as transfers, so `allow_negative` in the body is a 400.
- **Response:** `201 Created` with `{ "id": 12, "account_name": "customer:alice", "currency": "USD", "allow_negative": false }`. It does not include a balance, which is always 0 at creation and is read through `GET /accounts/{id}/balance`.
- **No idempotency key:** retrying a create whose response was lost returns 409 for the duplicate name, and there is no lookup endpoint. This is a known limit; no money moves, so a duplicate is harmless.

**Transfer request (`POST /transfers`)**

```json
{
  "from_account_id": 12,
  "to_account_id": 34,
  "amount_minor": 2500,
  "currency": "USD"
}
```

The `from` account gets an entry of `-amount_minor` and the `to` account gets `+amount_minor`. The idempotency key goes in the header, not the body.

- **Strict types:** `amount_minor` and the account ids must be JSON integers. Strings, floats, and booleans are rejected rather than coerced. The account ids must also be within the id bounds above.
- **Amount range:** `amount_minor` must be at least 1 and at most 99,900,000,000 (999 million dollars in cents), so a single transfer cannot overflow a `bigint` balance. Accumulated overflow would take about 92 million maximum-size transfers into one account, far beyond this project's scope.
- **Currency:** three uppercase letters. Lowercase is rejected, not normalized. Both accounts must have this currency; otherwise 400. The field is a deliberate guard: the client states what it believes it is moving.
- **Unknown fields are rejected**, so a typo such as `ammount_minor` is an error and not silently ignored.
- **Validation errors return 400, not FastAPI's default 422**, because 422 is reserved for insufficient funds.
- **Reverse takes no body:** `POST /transfers/{id}/reverse` rejects a request that has one.

**Error behavior to design up front**

- Same idempotency key with the same body: return the original response, rebuilt from the stored transaction and its entries, and move nothing.
- Same key with a different request (method, path, or body): reject with 409, since the client has a bug.
- A retried reversal (same key, same target) violates both the key's and the reversal link's unique constraints, and Postgres does not guarantee which one it reports. The claim therefore resolves a key conflict first (for example `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`), so the retry replays the original response; only a conflict on the reversal link under a different key is `AlreadyReversedError`.
- Same key while the first request is still running: the second request waits on the unique key, then replays the result or returns 409 on a different body. If the first request rolls back instead, the second claims the key for itself.
- How the claim decides (ADR 0012): `claim_transaction` returns `Claim(transaction_id, replay)`. It runs `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`; when no row comes back it reads the stored row for that key and compares request hashes, giving a replay (same hash) or `IdempotencyConflictError` (different hash, with a fixed message that does not repeat the key or the hashes). The conflict target is the key only, so a clash on the reversal link is still a unique violation until milestone 5 maps it to `AlreadyReversedError`.
- A failed attempt does not consume the key: the key row and the transfer share one database transaction, so a failure rolls both back. Error responses (400, 404, 422) are not stored, and a retry is evaluated against current balances.
- Insufficient funds: 422, and no entries are written.
- Unknown account: 404, and no entries are written.
- Amount zero, negative, or above the maximum; currency mismatch; malformed body, wrong types, or unknown fields: 400.
- Transfer from an account to itself: 400.
- Account created with a name that already exists: 409.
- Missing `Idempotency-Key` header, or one outside 1 to 255 characters (or containing a NUL character or a lone surrogate): 400.
- An id in a body, path, or cursor that is not an integer from 1 to 9,223,372,036,854,775,807: 400.
- Checks run in a fixed order so a request with several problems always gets the same error:
  1. Stateless checks, which never touch the database: header and body validation (400) and the same-account check (400).
  2. Claim the idempotency key. A replay returns the original response without re-evaluating balances, and the same key with a different request returns 409. Both end the request here.
  3. Unknown account (404), then currency mismatch (400).
  4. Lock the balances rows, then the insufficient-funds check (422).

  A failure at step 3 or 4 rolls back the key row, so a retry is evaluated fresh.
- Reversal of an unknown transfer: 404. Reversal of a transfer that is already reversed, or of a reversal: 409.
- Reversal that would overdraw a protected account because the money has moved on: 422, and no entries are written. A reversal follows the same rules as any transfer.
- Lock timeout, deadlock retries exhausted, or a wait for a database connection that times out: 503 with a Retry-After header. The transaction rolled back, so retrying with the same key is safe. The same holds if the connection drops during commit: the client cannot know whether the transfer happened, and retrying with the same key is safe either way. A request rejected by the overall concurrency cap also gets a 503 with Retry-After; no transaction has started, so retrying is safe.
- A violation of a database safety net, which signals a bug rather than a normal outcome: the balances CHECK (a normal overdraft is a 422), or the zero-sum or no-entries constraint trigger (a normal transfer is balanced by construction). The response is a 500, the transaction rolled back, and the error logged. The triggers fire at commit, so the key row rolls back with the transfer and the key stays free to reuse. These errors are not retried.

**Error model**

Expected failures are `LedgerError` subclasses in `ledger/errors.py`. The API layer owns the HTTP status for each, in one table resolved through the class hierarchy, so the domain code stays free of HTTP; a completeness test asserts that every concrete `LedgerError` subclass has a status. Bugs are not `LedgerError`s: `LedgerInvariantError` (with `UnbalancedEntriesError` under it) and the database safety-net violations (the balances CHECK and the two triggers) are never treated as expected outcomes; they return a 500 and are logged.

| Error | Status | `type` (after `urn:ledger:error:`) | `title` | Raised by |
| --- | --- | --- | --- | --- |
| `InvalidRequestError` | 400 | `invalid-request` | Invalid request | The API layer only: Pydantic validation (strict types, unknown fields, ranges, id bounds, same-account), with `RequestValidationError` remapped to 400, plus checks Pydantic cannot express (cursor decoding, no body on reverse). The service layer never raises it |
| `UnknownAccountError` | 404 | `unknown-account` | Unknown account | The service layer |
| `UnknownTransferError` | 404 | `unknown-transfer` | Unknown transfer | The service layer (`GET /transfers/{id}` and reverse) |
| `CurrencyMismatchError` | 400 | `currency-mismatch` | Currency mismatch | `post_transaction` |
| `InsufficientFundsError` | 422 | `insufficient-funds` | Insufficient funds | `post_transaction`; carries the account, its balance and the net change |
| `IdempotencyConflictError` | 409 | `idempotency-conflict` | Idempotency key reused | `claim_transaction` |
| `AlreadyReversedError` | 409 | `already-reversed` | Transfer already reversed | The claim, from the unique link on `reverses_transaction_id` |
| `ReversalOfReversalError` | 409 | `reversal-of-reversal` | A reversal cannot be reversed | The service layer, because the target has a non-null `reverses_transaction_id`; the schema allows such a row, so this is not a database rule |
| `DuplicateAccountNameError` | 409 | `duplicate-account-name` | Duplicate account name | `create_account`, from the unique name |
| `ServiceBusyError` | 503 | `service-busy` | Service busy | `run_in_transaction`: a pool wait timeout (milestone 3), then a lock timeout, exhausted deadlock retries and a connection lost during commit (milestone 4) |

The overall concurrency cap also returns a 503 with Retry-After, but the middleware answers it directly before any transaction starts, so it is not a `LedgerError`.

**Error response format**

Every error response is an RFC 9457 problem (`application/problem+json`), described in ADR 0009.

- **Members:** `type`, `title`, `status`, `detail` and `instance`, plus extension members. There is no `code` member. `type` is the URN `urn:ledger:error:` plus the name in the table, stored in the API-layer table and not derived from the class name; `title` is the fixed string from the table.
- **Domain errors:** `detail` is the exception's message. Extension members carry the data the error already holds: `account_id`, `balance_minor` and `net_change_minor` (the change the transfer would make to that account's balance) for `InsufficientFundsError`, and `account_name` for `DuplicateAccountNameError`. A `ServiceBusyError` also sends a `Retry-After` header.
- **Validation errors** (400): an `errors` list. A body problem has a `pointer` (an RFC 6901 JSON Pointer such as `/amount_minor`, or the empty pointer for a problem with the whole body, including malformed JSON); a path, query or header problem has a `parameter` naming it. A header is named in lowercase, as FastAPI reports it (`idempotency-key`), because header names are case-insensitive (RFC 9110). The client's input is never echoed back.
- **HTTP errors** from Starlette or a route (an unknown route, a wrong method): the same shape, with type `about:blank` and the HTTP status phrase as the `title`; a 405 keeps its `Allow` header. A status that cannot carry a body (1xx, 204, 205, 304) is sent with no body at all. A status code Python has no name for gets the title `HTTP Error`, and a `detail` that is not a string is converted to one.
- **Bugs:** one catch-all handler for anything that is not a `LedgerError` or a 503 class returns 500 with type `about:blank`, the title "Internal Server Error" and a fixed generic `detail` that never repeats the exception.

**Request id and logging**

- **Request id:** the server generates a UUID for every request and returns it in the `X-Request-ID` header and as `instance` (`urn:uuid:<id>`). It is attached to every log record made during the request, and log lines are formatted with it (`time level logger [request id] message`, set up once at startup by `configure_logging()` in `ledger/infra/logging_config.py`, which scripts such as the reconciliation job can reuse), so a response's `instance` can be found in the log. Outside a request the id shows as `-`. A client-supplied id is ignored, because ids are normally set by a gateway or load balancer and there is none in front of this service.
- **Logging is minimal:** a bug is logged once at ERROR with the exception and the request id (Starlette re-raises the exception after the catch-all handler has answered, so uvicorn would log it a second time without the id; `configure_logging()` drops that one record, and a test against a real uvicorn server counts the ERROR records); a 503 is logged at WARNING with its cause; expected 4xx errors and successful requests are not logged. Once the pool and concurrency settings are wired (the pool-wiring task below), one line at startup records them. Only the standard library is used.

**Request hash (`request_hash`)**

The hash is computed over a normalized form of the request, so a harmless difference between retries never looks like a different request.

- **Form:** the SHA-256, as 64 lowercase hex digits, of the canonical JSON (sorted keys, no spaces) of the list `[METHOD, path, body]`, computed by `request_hash(method, path, body)` in `ledger/api/rest/idempotency.py`. The form is a contract, because stored hashes are compared with new ones on every replay; a test pins it. A CHECK on `transactions.request_hash` (migration 007) enforces the 64 lowercase hex digits as a safety net.
- **Included:** the uppercase method, the path the request was routed on (so a reversal's target transfer is part of the hash; no trailing-slash or query-order differences), and the body as the validated request model dumped to JSON, or `null` when there is no body. Because the body is validated first, an omitted optional field and the same field set to its default hash the same. Text is not normalized, so two strings the database stores as different names are different requests.
- **Excluded:** the `Idempotency-Key` header itself, which is the lookup key and not part of the request's meaning, and other headers such as user agent or timestamps, which change between retries.

**Transfer response**

A replay must return exactly what the original request returned, so the response contains only what can be rebuilt from the stored transaction and its entries.

- **Body:** the transaction `id`, `reverses_transaction_id` (null for a normal transfer, so a reversal is recognizable), `created_at`, and the list of entries (`id`, `account_id`, `amount_minor`, `currency`). `GET /transfers/{id}` returns the same shape. `created_at` is ISO 8601 in UTC with a trailing `Z` (for example `2026-10-13T10:11:12.123456Z`), whatever time zone the database session uses. The entries are in the order they were written (by entry id). A transfer is visible only once its transaction has committed, and always with all of its entries, never part of one; a transaction found with no entries is a bug (a logged 500), not an empty transfer. An unknown id is a 404, and an id that is not plain decimal digits from 1 to 9,223,372,036,854,775,807 is a 400.
- **Not included:** account balances, which change after the transfer and could not be reproduced on a replay.
- **Status code:** a replay returns the same status code as the original (201 for a created transfer), not a different one.

## Milestones

Seven milestones, built in order. Finish each milestone before starting the next; each ends with something that can be demoed.

A ticked item means that piece of work is done and tested, not that the user-facing feature works end to end: a service function can be ticked before any route calls it. The milestone's "Done when" line is the acceptance check for the whole milestone.

### 1. Foundation

- [x] Repo, README stub, `docker compose up` starts Postgres and the app
- [x] Schema migrations for accounts, transactions (including the nullable, unique reverses\_transaction\_id, which is a foreign key to transactions), and entries; balances arrives in milestone 2. Entries get a composite foreign key on (account\_id, currency) so an entry's currency must match its account's, and migrations run as an owner role while the service connects as an app role that owns no tables and has no UPDATE, DELETE, or TRUNCATE on entries. Entries also get a CHECK that amount\_minor is nonzero, a foreign key from transaction\_id to transactions, an index on transaction\_id (Postgres does not index foreign key columns automatically) for the lookups by transaction (`GET /transfers/{id}`, rebuilding a replay response, the zero-sum trigger, and the no-entries check), and an index on (account\_id, id) for entry pagination, and accounts get a unique (id, currency) to back the entries foreign key, a unique (id, allow\_negative) to back the balances foreign key added in milestone 2, and a unique name, with the app role limited to INSERT and SELECT on accounts
- [x] Grants are explicit per table, written in the migration that creates the table, with no `ALTER DEFAULT PRIVILEGES`; the service never connects as the superuser or the owner role, and the app role is created before migrations run (Docker init script locally, the same step in CI). A test asserts each table's exact privileges for the app role (entries and transactions allow only SELECT and INSERT; accounts allows only SELECT and INSERT; balances, once it exists in milestone 2, allows SELECT, INSERT, and UPDATE) so a stray grant fails the build
- [x] pytest running against a real Postgres, not mocks, in a separate `ledger_test` database so a test run never empties the data of the running service (created by `docker/roles/roles.sql`); fixtures connect as the owner role to reset and seed data, and as the app role for everything under test
- [x] GitHub Actions runs lint and tests (with a coverage floor) on every pull request and every push to main, plus a Docker Compose smoke job that builds the image and checks `/health`; both checks are required to merge

**Done when:** a fresh clone passes CI.

### 2. Double-entry core

- [x] `claim_transaction(conn, idempotency_key, request_hash, reverses_transaction_id=None)` inserts the `transactions` row and returns a `Claim` holding its id (milestone 3 added the conflict handling that makes it the idempotency claim, see Error behavior and ADR 0012), and `post_transaction(conn, transaction_id, entries)` rejects any set of entries that does not sum to zero per currency (raising `UnbalancedEntriesError`, a bug that maps to a 500; see Error model), checks the accounts and currencies, locks the touched balances rows, and writes the entries and balance updates. Both take the request's connection, never commit, and assume the caller has opened the database transaction (`post_transaction` raises if the connection is in autocommit mode), so the claim and the posting share one transaction and a failure rolls both back
- [x] Entries are append-only; no update or delete path exists in the code, and the database refuses them too for the app role
- [x] Balance computed from entries, with a cached balance in a separate balances table (account\_id as primary key; allow\_negative copied from the account, with a composite foreign key (account\_id, allow\_negative) to accounts and a CHECK (allow\_negative OR balance\_minor >= 0); added by a new migration that backfills a row for every existing account from its entries and the account's allow\_negative; if the existing entries would leave a protected account negative, the migration stops with an error that names the account, since the data already breaks an invariant), kept in the same database transaction
- [x] Overdraft rule for accounts where `allow_negative` is false: checked in `post_transaction` against the balance returned by the locking statement, which locks each distinct touched balances row with `SELECT ... FOR UPDATE` in ascending account id order (422), with the CHECK on balances as a backstop. The 500 response when the CHECK fires belongs to the milestone 3 error mapping
- [x] `create_account(conn, account_name, currency, allow_negative)` service function, which also creates the balances row (copying allow\_negative); the seed script or fixture uses it to create one allow\_negative funding account per currency, and the API endpoint calls it with `allow_negative` fixed to false
- [x] Deferred constraint trigger on entries that checks each transaction's per-currency sum at commit, as a second line of defense behind post\_transaction, plus a deferred check on transactions that rejects a transaction with no entries. Fixtures insert only balanced sets of entries from here on. The 500 response when either trigger fires belongs to the milestone 3 error mapping
- [x] Failure injection: raise an error between the inserts and the balance update inside `post_transaction` and assert the database is unchanged
- [x] Test the balances constraints directly: an UPDATE that sets a protected account's balance below zero is rejected by the CHECK, a balances row whose allow\_negative differs from its account's is rejected by the foreign key, and a funding account (allow\_negative true) may go negative
- [x] Property-based test (Hypothesis) over random sequences of valid and invalid posts across accounts with mixed currencies and `allow_negative` settings. After every step it checks that every transaction sums to zero per currency, the per-currency total across all accounts is unchanged, every cached balance equals the sum of its entries, no protected account is negative, and a rejected post leaves the entries and balances exactly as they were. It is a `RuleBasedStateMachine` on a real database: every accepted post really commits, so the deferred triggers run too, which rules out savepoints, and the database is reset when each example starts and ends (function-scoped fixtures run once per test, not once per example). It sets `deadline=None` and keeps `max_examples` modest (about 50)

**Done when:** unit tests cover the invariants, the property-based test passes, a failed post leaves the database unchanged, a direct UPDATE, DELETE, or zero-amount insert on entries is rejected, committing an unbalanced set of entries directly in SQL fails, and a direct UPDATE that sets a protected account's balance below zero is rejected.

### 3. API and idempotency

- [x] `POST /accounts`
- [x] `GET /accounts/{id}/balance`
- [x] `GET /transfers/{id}`
- [x] A fixed `psycopg_pool` pool of 10 connections with a 2-second wait for one, both read from environment variables
- [x] `Idempotency-Key` stored with a hash of the normalized request (see Request hash in the API section), so a reversal's target transfer is part of the hash; tests cover key order, whitespace, and explicit-default variants hashing the same. The service layer `claim_transaction` is done; `POST /transfers` is the first route to use it
- [x] Request validation per the Transfer request section: strict types, unknown fields rejected, the amount cap and the id bounds on bodies and paths, and validation errors mapped to 400 instead of FastAPI's default 422
- [x] Error mapping: each `LedgerError` subclass returns its status, `type` and `title` from the Error model table as an RFC 9457 problem with a request id (see Error response format), HTTP errors from Starlette use the same shape, and a bug returns a logged 500 with the transaction rolled back and no retry. Covered by a completeness test and by test-only routes, plus a commit-time failure through the real transaction helper
- [x] The OpenAPI description of the balance endpoint states that it reads the cached balance, may not include a transfer still in flight, and never shows part of a transfer
- [x] Move the transfer read side and the entry builder to `transfer_service.py` and `transfer_model.py` as ADR 0013 planned: `posting_service` loses (`get_transfer`, `entries_for_transfer`) and `posting_model` loses (`Transfer`, `TransferEntry`); `find_transfer` stays in `posting_repo.py`; `claim_transaction` and `post_transaction` stay in `posting_service`; there is no `transfer_repo.py`. A mechanical move with no behavior change, with the import paths in the tests and their docstrings updated
- [x] `POST /transfers`: require and validate the `Idempotency-Key` header (1 to 255 characters, no NUL or lone surrogate), hash the request, claim the key, post unless it is a replay, and return the stored rows (201 on replay too); the replay, mismatch, and failed-then-retried items below are its tests
- [x] Integration tests for the fixed order of checks in the Error behavior section: stateless checks, key claim, account and currency, then lock and funds, so a request with several problems always gets the same error
- [x] Replay: the same key and the same request sequentially returns the original response with the same status (201), rebuilt from the stored rows, and moves nothing a second time (entries and balances unchanged); a replayed reversal key is covered in milestone 5
- [x] Mismatch: the same key with a different method, path or body returns 409 with the `idempotency-conflict` type, moves nothing, and does not repeat the key or the hashes in the response
- [x] Failed-then-retried: a request that fails with a 400, 404 or 422 stores nothing, and a retry with the same key is evaluated fresh against current balances (for example a 422, then fund the account, then the same key succeeds)
- [ ] Concurrent same key: two simultaneous requests with the same key and body produce exactly one transfer and both get the same response; the same key with different bodies gives one success and one 409
- [ ] Failed transfers do not burn the key: the key is inserted inside the transfer's own database transaction, so a failure rolls it back; a test injects an unexpected exception after the key insert (not one of the safety-net cases above) and asserts the key is free to reuse
- [ ] Safety nets through `POST /transfers`: a `LedgerInvariantError` (unbalanced entries), the balances CHECK, the zero-sum trigger and the no-entries trigger each return a 500 `about:blank` problem with a generic detail, log the exception at ERROR, persist nothing, leave the same `Idempotency-Key` reusable, are not retried, and leave the pool healthy; a normal overdraft is the control and returns 422
- [ ] Entry history query for one account (service and repository layers): newest first by entry id (never by `created_at`), fetches `limit + 1` rows to learn whether another page exists, reads each entry's `created_at` by joining `transactions`; an unknown account is a 404 and a real account with no entries returns an empty page
- [ ] Entries cursor: the base64url of the last returned entry id, opaque and unsigned; undecodable text and an id outside the id bounds are both 400; unit-tested without a database
- [ ] `GET /accounts/{id}/entries` route per the API table: `limit` (default 50, 1 to 200, anything else a 400 and never clamped) and an optional `cursor`; the response is `{ "entries": [...], "next_cursor": "..." }` with `next_cursor` null on the last page and no total count; OpenAPI description written
- [ ] Walking every page of an account returns each entry exactly once, including an exact multiple of the limit, a single page, and an account with no entries
- [ ] A pool wait timeout becomes `ServiceBusyError` and a 503 with a Retry-After header, tested with an exhausted pool (the values are tuned in milestone 4)
- [ ] Handler thread limit set to 20 at startup, read from an environment variable
- [ ] Overall concurrency cap: an ASGI middleware, inside the request-id middleware, returns a 503 with Retry-After above the cap, read from an environment variable; tested by holding requests with a barrier
- [ ] One INFO line at startup records the effective pool, thread limit and concurrency cap settings
- [ ] OpenAPI: each route has a summary and description, and its error responses (`application/problem+json`) are documented; `/openapi.json` lists the five endpoints
- Note: `POST /transfers/{id}/reverse`, the sixth endpoint in the API section, arrives in milestone 5, after the locking from milestone 4

**Done when:** all five endpoints work against a real Postgres; the replay test and the concurrent same-key test both pass; every safety net returns a logged 500 and leaves the key reusable; walking every page of an account returns each entry exactly once; and a request beyond the pool or the concurrency cap gets a 503 with Retry-After.

### 4. Concurrency

- [ ] ADR 0014 (Draft): why row locks at READ COMMITTED and not SERIALIZABLE. It covers the overdraft invariant living on a single locked row, so a lock plus the check on the returned balance is enough; that SERIALIZABLE would add 40001 serialization failures, and retries for them, on ordinary conflicts such as a hot account; and what would change the decision. Update the "to come" notes in ADR 0006, ADR 0012 and `docs/adr/README.md`
- [ ] The pool's connections run at READ COMMITTED: a test reads `transaction_isolation` inside `run_in_transaction` and asserts it, so a changed default cannot silently change the locking behavior
- [ ] Concurrency test harness in `tests/concurrency`: runs N callables in threads, each with its own connection from a test pool of at least 50, released together by a barrier with no sleeps; collects each thread's result or exception; resets the database between runs and logs the random seed. The check that the ledger invariants hold (per-currency total, no protected account negative, every balance equal to the sum of its entries) is moved out of the property test into a shared helper that both use
- [ ] Opposing transfers (A to B and B to A) fired together by a barrier, many times, directly at the service layer with no retry, never raise a deadlock error (SQLSTATE 40P01). It runs without the retry on purpose, because a retry would hide a locking-order bug
- [ ] Negative control for that test: a test-only lock helper, kept with the test helpers and never in `src`, takes the two balances rows in request order, one statement per account, with a barrier between the first and second lock. It deadlocks under the same test, so the test is shown able to fail
- [ ] No double spend on one account: two concurrent transfers that together exceed the balance produce exactly one success and one insufficient-funds, because the overdraft check reads the balance returned by the locking statement (`SELECT ... FOR UPDATE` in ascending account id order, built in milestone 2), never an earlier plain SELECT
- [ ] 50 concurrent transfers across a few accounts never overdraw and never change the total. This calls the service layer directly from 50 threads. After each run, the shared invariant check passes and the entry count matches the successful transfers; run 20 times as 20 parametrized runs, each with its own logged seed
- [ ] Deadlock retry in `run_in_transaction`: on SQLSTATE 40P01, roll back and re-run the whole `work` function (so the key claim is redone) up to 3 attempts with a random delay of roughly 10 to 50 ms; exhausting the attempts raises `ServiceBusyError`; any other error is not retried
- [ ] Lock timeout: `SET LOCAL lock_timeout` of 2 seconds at the start of each write transaction, so a stuck request fails instead of hanging; the lock-not-available error (55P03) becomes `ServiceBusyError` and is not retried, because a stuck holder would only be hit again (moved from milestone 3, since it can only be tested once the setting exists)
- [ ] Connection lost during commit becomes `ServiceBusyError`; the client cannot know whether the transfer happened, and retrying with the same key is safe either way
- [ ] API-level 503s: each of the three causes (exhausted deadlock retries, lock timeout, connection lost during commit) returns a 503 `service-busy` problem with a Retry-After header, the Retry-After value is settled here, and any other database error still returns the logged 500. A connection-pool wait timeout returns the same 503, mapped in milestone 3
- [ ] Retry attempts, delay bounds and the lock timeout are read from environment variables, and the startup INFO line includes them. The values are starting points to tune from load-test results
- [ ] Paging while transfers run: one reader walks an account's entries in small pages while writer threads post transfers into and out of it (via the harness, at the service layer); no entry appears twice, ids strictly decrease, and every entry committed before the walk started is returned
- [ ] Control for that test: a deterministic test where one writer inserts an entry without the balances lock and commits after a later one, showing a page boundary can skip it; this is why every writer locks the balances row first
- [ ] Server-process fixture: starts `uvicorn ledger.main:app` as a subprocess on a free port, with environment variables pointing it at `ledger_test`, waits for `/health`, captures its output, and stops it at teardown
- [ ] End-to-end concurrency test over HTTP: 50 client threads released together by a barrier, each sending a transfer with its own `Idempotency-Key`; every response is 201 or 422, with no 5xx; afterwards the shared invariant check passes (read through the owner connection) and the entry count matches the number of 201s. The fixture sets the concurrency cap above 50 and the pool wait (and, once it exists, the lock timeout) well above anything CI could hit, for example 10 seconds, under the 30-second test timeout, so a 503 here would mean a bug; the test also asserts that the captured server output has no WARNING lines
- [ ] A script that runs the `tests/concurrency` folder 20 times in a row and stops at the first failure, documented in the README

**Done when:** the 50-transfer test passes all 20 of its parametrized runs; the whole `tests/concurrency` folder passes 20 times in a row with no flaky failure; the opposing-transfers test never reports a deadlock while its negative control does; deadlocks, lock timeouts and a lost commit connection each return a 503 with Retry-After; and the HTTP concurrency test and the paging test pass.

### 5. Reversals

- [ ] Mirrored entries: a named builder (like `entries_for_transfer`) takes the original transfer's entries and returns one entry per original, with the same account and currency and the amount negated (full reversals only); its output always sums to zero; unit-tested
- [ ] `reverse_transaction` service function: loads the target (unknown is `UnknownTransferError`), refuses a target that is itself a reversal (`ReversalOfReversalError`, a service rule since the schema allows such a row), claims the key with `reverses_transaction_id`, and posts the mirrored entries through `post_transaction`, so locking, currency checks and the overdraft rule are the same as for any transfer
- [ ] One reversal per transfer: a unique violation on `reverses_transaction_id` under a different key becomes `AlreadyReversedError`, while a same-key retry is still resolved first as a replay; the mapping uses the constraint's existing Postgres-generated name, `transactions_reverses_transaction_id_key`, and a test reads the name from `pg_constraint` so a rename cannot silently turn the 409 into a 500; update the two milestone 3 tests that pin the raw violation
- [ ] Funds moved on: a reversal that would overdraw a protected account returns 422 (`InsufficientFundsError`), writes nothing, and leaves the transfer reversible later
- [ ] `POST /transfers/{id}/reverse` route: requires `Idempotency-Key`, rejects a body (400), strict path id, the target id is part of the request hash, 201 with the transfer response shape, a replay returns the same response; OpenAPI description written
- [ ] Reversal rules over HTTP: reversing twice (different keys) gives 409 `already-reversed`; reversing a reversal gives 409 `reversal-of-reversal`; an unknown transfer gives 404; and replaying a reversal's own key returns the original 201 and moves nothing a second time
- [ ] Racing reversals (via the concurrency harness): two reversals of one transfer under different keys give exactly one 201 and one 409, and the balances are restored exactly once; two under the same key give one reversal and the same response to both
- [ ] Property test: extend the existing Hypothesis state machine with a reverse rule; after every step the shared invariant check holds, and a transfer followed by its reversal restores every touched balance when nothing else touched those accounts in between

**Done when:** a transfer plus its reversal restores every balance; a reversal after the funds moved on returns 422 and leaves the transfer reversible; replaying a reversal's key moves nothing a second time; and two racing reversals produce exactly one success and one 409.

### 6. Reconciliation

- [ ] Balance drift query: one statement compares each account's cached balance with the sum of its entries and returns `Drift(account_id, cached_minor, computed_minor)` for every mismatch, including an account with no balances row; a clean database returns none; a balance corrupted through the owner role is caught; an account with no entries and a zero balance is clean
- [ ] Snapshot: the job runs its checks in one read-only REPEATABLE READ transaction, so live traffic cannot cause false drift; a test holds the snapshot open, commits a transfer on another connection, and shows the job reports no drift; a control runs the same two statements at READ COMMITTED and shows false drift. One statement is already consistent at READ COMMITTED; the snapshot matters because the job runs several statements that must agree
- [ ] Extra integrity checks in the same snapshot: every transaction sums to zero per currency, every transaction has entries, and the sum of all balances in each currency is zero
- [ ] Job entry point `python -m ledger.reconcile`: connects as the app role, uses `configure_logging()`, prints one line per mismatch, and exits 0 when clean, 1 when drift is found, and 2 on an error; `reconcile` is added to the entry points the layering test allows (`ENTRY_POINTS` in `tests/unit/layering_scan.py`); documented in the README
- [ ] Statement format in the design doc: one statement covers one account (`--account ID`), the file has a header row and the columns `transaction_id,amount_minor,currency` with integer minor-unit amounts, `--from` and `--to` (both required, UTC ISO 8601, from inclusive and to exclusive) select the account's entries by `transactions.created_at`, and a difference is one of three kinds: an entry in the ledger but not on the statement, a statement line with no ledger entry, or a matching `transaction_id` with a different amount or currency. A transaction id repeated on the statement is a file error, and a transaction with two entries for the one account is reported as an error, not matched
- [ ] Statement CSV parser (pure, unit-tested): strict header, integer amounts (a decimal such as `12.50` is rejected), currency format, repeated transaction ids rejected; every malformed row is an error that names its line number, and nothing is compared when the file is malformed
- [ ] Statement comparison: given the parsed lines and the account's entries in the window, returns the differences by kind; tested against the database with each kind
- [ ] Job integration: `python -m ledger.reconcile --statement FILE --account ID --from T --to T` runs the comparison in the same snapshot as the balance check, prints one line per difference, and uses the same exit codes (0, 1, 2); a malformed file exits 2
- [ ] Round trip: a statement generated from the ledger itself shows no differences, and changing, deleting or adding one line gives exactly one difference of the right kind

**Done when:** a deliberately corrupted balance is caught by the job, which exits 1 and names the account; a transfer committed during a run does not cause false drift; and a statement generated from the ledger shows no differences, while a changed, missing or extra line is reported.

### 7. Polish and proof

- [ ] Seed script, deferred until now: until the load test needed one, funding was added by hand to the local database and the test suite used fixtures. It creates N accounts per currency plus one `allow_negative` funding account per currency through the service's `create_account` (as the app role, never the owner), funds the accounts with transfers from the funding account so that transfers do not all draw from one funding account (that would serialize every request on a single balances row), and writes the account ids to a file the load test and the demo can read; the account count and a random seed are arguments
- [ ] Locust workload: each request is a `POST /transfers` with its own `Idempotency-Key`; two shapes chosen by an option, uniform (any account to any other) and skewed (Zipf with exponent s = 1 by default, s a command-line option, where the account of popularity rank k is chosen with probability proportional to 1/k^s and a draw of the same account for both ends is redrawn); accounts are funded enough that 422s are rare, and a 422 or any 5xx is counted and shown separately
- [ ] Benchmark runner script: runs Locust headless for a fixed time and user count and writes a results file with the exact command, git commit, hardware spec (CPU, RAM, OS), account count, workload shape (for the skewed shape, s and the measured share of the busiest account and of the busiest 20% of accounts), the pool, thread and cap settings from the startup log line, selected Postgres settings (`SHOW`), and whether Locust shares the machine with the service
- [ ] Recorded runs: uniform and skewed results (throughput and latency percentiles) added to the README Benchmarks section, each with the command that produced it
- [ ] Review the pool size (10), thread limit (20) and concurrency cap from those results by running a few values; record the chosen values and why in the design doc, and as an ADR if the choice is large
- [ ] Trigger toggle in the benchmark runner: `--zero-sum-trigger on|off` disables and re-enables `tbl_entries_zero_sum_integrity` as the owner role with `ALTER TABLE entries DISABLE TRIGGER` and `ENABLE TRIGGER`, so the migration history is untouched; it re-enables in a `finally`, verifies the state from `pg_trigger` afterwards, and refuses to start if the trigger is already disabled; tested, including that a failed run leaves it enabled
- [ ] Consistency after a trigger-off run: the milestone 6 integrity checks run after the run and show that every transaction still sums to zero
- [ ] Repeated runs: each of the two settings runs several times on identically seeded data, alternating order, under both the uniform and the skewed shape; report the median and the spread
- [ ] Recorded cost of the zero-sum trigger: the difference in throughput and latency, with the commands, added to the README Benchmarks section, with a plain explanation that the trigger fires at commit while the row locks are held. Measuring the no-entries trigger (`tbl_transaction_entries_integrity`), alone or together with this one, is an option for later
- [ ] Compose one-shot `reconcile` service: like `migrate`, it runs `python -m ledger.reconcile` against the app database as the app role and exits with the job's exit code, so `docker compose run --rm reconcile` works for the demo and the README
- [ ] Design-decisions page `docs/design-decisions.md`, covering integer cents, isolation level with the locking, and idempotency. It is a reader-facing synthesis of the ADRs and makes no new decisions. Each topic is one short section: the problem, what we chose, what it costs (with the measured number from the benchmarks where there is one, such as the hot-account result for locking), and which tests defend it, with a link to its ADR. The idempotency section says in one sentence that the retry wraps the key claim and the posting together because the caller owns the transaction, linking ADR 0005. A note says `docs/design.md` is the source of truth if the two ever differ
- [ ] README architecture section: embeds `docs/ledger-architecture.svg`, checked against the layered code (ADR 0013) and the reconciliation job, with a short walk-through of one transfer from request to commit
- [ ] README Correctness guarantees in the present tense: each invariant links to the tests that defend it
- [ ] README Design decisions: a short summary of the main decisions that links to the design-decisions page and the ADR index
- [ ] README Known limits: the limits from milestones 3 to 7 added in plain terms (for example the hot-account bottleneck, retry and 503 behavior, reconciliation as a batch job, no deposit endpoint, and the statement CSV rules)
- [ ] README how to run: covers `docker compose up`, the seed script, a transfer example, `docker compose run --rm reconcile`, the load test and the benchmark runner, and the tests; every command copied from one that was run
- [ ] Demo outline `docs/demo.md`: five beats of about 25 seconds each, with what to say and what appears on screen, timed to fit 2 minutes, with the setup steps (stack up, seed) listed separately and done before the clock starts. The beats are: a transfer and a replay of the same key; the same key with a different amount (409) and an overspend (422); a burst of concurrent transfers with no negative balance and an unchanged total; a reversal that restores the balances; and reconcile reporting clean, then a balance corrupted by hand as the owner role, then reconcile catching it
- [ ] Demo runner `scripts/demo.py`, in Python with only the standard library so it runs the same on Windows and Linux: prints each request before sending it and the response after, waits for Enter between beats, has a `--no-pause` mode, and does the corruption beat with a printed owner-role `psql` command
- [ ] Rehearsal check: a test runs the demo with `--no-pause` against the server-process fixture from milestone 4 and asserts every beat gives the expected status and result, so the demo cannot silently break
- [ ] Fresh-clone check: clone into a clean directory, follow only the README, and get the service running, a transfer made and the reconcile run, timing it against the 10 minutes; fix anything unclear; then update the README status line (all milestones complete) and the API table (all Done)

**Done when:** following only the README in a clean clone, the service is running, a transfer is made and the reconcile job has run within 10 minutes; the architecture section and the design-decisions page explain why the ledger stays correct; every benchmark number quoted has the command that produced it; and the demo runs end to end in under 2 minutes.

## Testing and correctness

The tests are the proof that the ledger is correct. Each kind of test targets a different failure.

| Test type | What it catches | How |
| --- | --- | --- |
| Unit | Single-rule bugs in the stateless checks (zero, negative, and over-cap amounts, strict types, unknown fields, ids of 0, negative, or above the bigint maximum, and same-account transfers) | Plain pytest, no database |
| Layering | Imports that cross the layers of ADR 0013 | Tests that read the source tree with `ast` and check which layer may import which, and the structure inside the API layer; no database |
| Integration | Database-dependent rules and the order of checks | Pytest against a real Postgres: currency mismatch, unknown accounts, and the fixed order of checks (stateless, key claim, account and currency, lock and funds) |
| Property-based | Invariant violations nobody thought of | Hypothesis (milestone 2; a state machine that resets the database when each example starts, `deadline=None`, about 50 examples): random sequences of transfers (and, from milestone 5, reversals); after every step the shared invariant check holds: the per-currency total across all accounts stays zero (funding accounts go negative by what they issue), every cached balance equals the sum of its entries, and no protected account goes negative |
| Concurrency | Races, deadlocks, double spends | A shared harness runs service-layer threads, each with its own connection, released together by a barrier with no sleeps: 50 transfers at once in 20 parametrized runs, each with a logged seed; opposing A-to-B and B-to-A transfers with no retry, so a retry cannot hide a deadlock; two transfers that together overspend one account; and paging through an account's entries during concurrent transfers to show no entry is skipped. One end-to-end run goes over HTTP against a server subprocess, and a script runs the whole folder 20 times in a row |
| Failure injection | Half-written transactions | Raise an error between the inserts and the balance update, and another unexpected exception after the key insert, and assert the database is unchanged and the key is free to reuse; fire each database safety net through `POST /transfers` and assert a logged 500 |
| Idempotency | Duplicate charges on retry | Replay the same key sequentially and concurrently, send the same key with a different request (409), and retry a failed request with the same key |
| Overload and timeouts | Requests that hang or fail without a clear answer | An exhausted pool, the concurrency cap, a lock timeout, exhausted deadlock retries and a connection lost during commit each return a 503 with Retry-After; any other database error is still a logged 500 |
| Reversal | Double or racing reversals, balances not restored | Reverse twice (second gets 409), fire two reversals at once (exactly one wins), reverse after funds moved (422), replay a reversal's key (nothing moves twice), and a property test that a transfer plus its reversal restores every balance |
| Reconciliation | Silent drift | Corrupt a cached balance by hand and confirm the job flags it and exits 1; commit a transfer during a run and confirm there is no false drift, with a READ COMMITTED control that shows the false drift; run the extra integrity checks; and compare a statement generated from the ledger, which shows no differences, while a changed, missing or extra line is reported |
| Load | Throughput and latency claims | Locust against a local Postgres with a uniform and a Zipf (s = 1) workload; record the machine spec and the workload shape; repeat the runs with and without the zero-sum trigger and check the data is still consistent |
| Demo | The live demo breaking | A test runs `scripts/demo.py --no-pause` against a server subprocess and asserts every beat |

The tests are laid out by kind: `tests/unit` (no database), `tests/integration` with `db` (schema, constraints and the repository functions), `service` (the business rules) and `api` (the HTTP contract only), `tests/property`, and `tests/e2e` for the tests that cross layers on purpose (the whole app, concurrent requests, the commit-time safety net), with a `tests/concurrency` folder to come in milestone 4 for the shared harness and the service-level concurrency tests (the HTTP concurrency test stays in `tests/e2e`). Only tests that use a database fixture need a running Postgres (ADR 0010), and they run in the `ledger_test` database, never in the service's own `ledger` database.

Where a test would pass on code that already works, a control shows the test can fail: a test-only lock helper that locks in the wrong order and deadlocks under the opposing-transfers test, a READ COMMITTED run that shows the false drift the reconciliation snapshot prevents, and an entry written without the balances lock that a page boundary skips.

Request validation is checked against the database: a property test generates arbitrary names, currencies, keys and numbers, including NUL characters, lone surrogates, a trailing newline and values just outside the `bigint` range, and requires that anything the API accepts can be stored. The API may be stricter than the database's CHECK constraints, never looser, so a valid-looking request cannot become a logged 500. Another test lists every CHECK constraint in the schema, so a new one cannot be added without deciding who guards its values.

Every benchmark number quoted in the README comes with the command that produced it. A number nobody can reproduce is worse than no number.

## Stretch goals

Only after milestone 7. The out-of-scope list above is final; new ideas go here.

- Outbox table that publishes events for each posted transaction
- Multi-currency with an explicit exchange-rate entry
- A gRPC interface alongside REST
- Rate limiting on the transfer endpoint
- Partial reversals (the scope covers full reversals only)
