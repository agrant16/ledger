# ADR 0009: API error responses follow RFC 9457, with a request id on every response

* **Status:** Accepted
* **Date:** 2026-10-11 (decided during milestone 3 planning)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
ADR 0007 decides which failures are expected (`LedgerError`) and which are bugs, and that the API layer owns the HTTP status for each. It leaves open what an error response looks like. The design reserves 422 for insufficient funds, so FastAPI's default validation error cannot be used, and a client needs a stable way to tell one 409 from another. A bug must return a response that helps whoever reads the logs without telling the client anything about the internals. This is a personal, public project with no authentication and no production deployment, which shapes how much a response needs to hide.

## Decision
- **Every error is an RFC 9457 problem** with the media type `application/problem+json` and the members `type`, `title`, `status`, `detail` and `instance`, plus extension members. There is no separate `code` member: `type` is the machine-readable identifier.
- **`type` is a stable URN** such as `urn:ledger:error:insufficient-funds`, stored explicitly in the API-layer table next to the status and the fixed `title`. It is not derived from the class name, so renaming a class cannot change the public API. A completeness test checks that every `LedgerError` has a row and that types and titles are unique.
- **`detail` and extension members.** For domain errors, `detail` is the exception's message and the members are the data the error already carries (`account_id`, `balance_minor` and `amount_minor` for insufficient funds; `account_name` for a duplicate name). Nothing is stripped: this is recorded in the README's known limits.
- **Validation errors** are status 400, type `urn:ledger:error:invalid-request`, with an `errors` list. A body problem carries an RFC 6901 JSON Pointer (`/amount_minor`; the empty pointer for a problem with the whole body, including malformed JSON). A path, query or header problem carries `parameter`. The client's input is never echoed back.
- **HTTP errors from Starlette or a route** (an unknown route, a wrong method) use the same shape with type `about:blank`, the HTTP status phrase as the `title`, and any headers the error carries, such as `Allow` on a 405. A status that cannot carry a body (1xx, 204, 205, 304) is sent with no body. A status code Python has no name for gets the title `HTTP Error`, and a `detail` that is not a string is converted to one.
- **Bugs.** One catch-all handler takes everything that is not a `LedgerError` or a 503 class, including non-database exceptions. It returns status 500 with type `about:blank`, the title "Internal Server Error" and a fixed generic `detail`.
- **Request id.** Each request gets a server-generated UUID, returned in the `X-Request-ID` header and as `instance` (`urn:uuid:…`), and attached to every log record made during the request. Log lines are formatted with it (`configure_logging()` in `ledger/logging_config.py`, called from the lifespan), so a response can be matched to its log entry; outside a request the id shows as `-`. A client-supplied id is ignored.
- **Minimal logging.** A bug is logged once at ERROR with the exception and the request id, and a 503 at WARNING with its cause. Expected 4xx errors and successes are not logged. One startup line records the effective pool and concurrency settings; it arrives with the pool wiring, because the concurrency cap does not exist before then. There is no logging library beyond the standard library.

## Consequences
* **Positive:**
  - Clients branch on one stable field, and the four 409 errors are distinguishable.
  - A bug report can be matched to its log entry through the request id, and the response never leaks SQL, constraint names or class names.
  - The status table, the `type` strings and the titles are one reviewed list, with a test that fails if a new error is left out.
  - Every response, including a 404 for an unknown route, has the same shape.

* **Negative / Trade-offs:**
  - Every new `LedgerError` needs a table row with a type and title (the completeness test turns a forgotten row into a failing test).
  - Error responses expose balances and account ids. That is acceptable only because the project has no authentication and is not a production service.
  - The catch-all handler runs outside the middleware stack in Starlette, so the 500 response must set the request id itself, and the id must still be available when the handler runs.
  - The `Retry-After` value on a 503 is not decided here; it is settled with the pool wiring.

* **Alternatives Considered:**
  - **`about:blank` for every error:** rejected, because a client could not tell the 409s apart.
  - **A type derived from the class path (`errors:LedgerError:InsufficientFundsError`):** rejected. It ties the wire format to Python class names, and the hierarchy is flat, so the prefix is the same for every error.
  - **A resolvable URL as the type:** rejected, since the project has no domain to host documentation.
  - **A custom body such as `{"error": "insufficient_funds"}`:** RFC 9457 was chosen because it is a standard with an existing media type and member names.
  - **A separate `code` member:** dropped, because it would duplicate `type`.
  - **Stripping balances and ids from responses:** not done. It costs mapping code and tests and protects nothing in a public demo project.
  - **Honoring a client-supplied request id:** that is what a gateway or load balancer does, and there is none here. It would also let a client put text of its choosing into the logs.
  - **A structured-logging library, or logging every request:** rejected as noise. The ledger's entries are already the audit trail.
