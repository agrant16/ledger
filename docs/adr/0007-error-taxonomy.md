# ADR 0007: Expected failures and bugs are different kinds of error

* **Status:** Accepted
* **Date:** 2026-10-09 (decided during milestone 2 planning; implemented 2026-10-10)
* **Deciders:** Alan (human architect) & Claude (AI collaborator)

## Context
The service has to turn failures into HTTP responses, and not every failure is the same kind of thing. An overdraft is a normal outcome and returns a specific 4xx. An unbalanced set of entries from `post_transaction` means the code has a defect, since a normal transfer balances by construction. The design also reserves 422 for insufficient funds, so validation errors cannot use FastAPI's default.

## Decision
- **`LedgerError` is the base of every expected, domain-level failure**, in `ledger/errors.py`: `InvalidRequestError`, `UnknownAccountError`, `UnknownTransferError`, `CurrencyMismatchError`, `InsufficientFundsError`, `IdempotencyConflictError`, `AlreadyReversedError`, `ReversalOfReversalError`, `DuplicateAccountNameError`, and `ServiceBusyError`.
- **`LedgerInvariantError` is a separate base, not a `LedgerError`,** for bugs. `UnbalancedEntriesError` sits under it. The database safety-net violations (the balances CHECK and the two constraint triggers) are never `LedgerError`s either. They return a logged 500, roll back the transaction, and are not retried.
- **The domain code is free of HTTP.** The API layer owns the status for each error in one table, resolved through the class hierarchy (a flat hierarchy with no intermediate bases). A completeness test asserts that every concrete `LedgerError` subclass has a status.
- **`InvalidRequestError` is raised only by the API layer.** Pydantic validates strict types, unknown fields, ranges, and id bounds, and `RequestValidationError` is remapped to 400. The service layer raises only state-dependent errors; the database constraints are the last line of defense.
- **Two reversal errors.** `AlreadyReversedError` comes from the unique link on `reverses_transaction_id`. `ReversalOfReversalError` is a service-layer check, since the schema allows such a row. Both are 409.
- **Errors carry the data that helps.** `InsufficientFundsError` has `account_id`, `balance_minor`, and `amount_minor`; `DuplicateAccountNameError` has `account_name`.
- **Database errors from CHECK constraints are not translated.** Validation belongs at the API boundary, so a CHECK that fires is a bug and surfaces as a logged 500.

## Consequences
* **Positive:**
  - The type system separates "the client did something we expect" from "we have a bug".
  - A generic `except LedgerError` (for example in a retry wrapper) never swallows a defect.
  - Tests and clients can target exactly one failure, and two reversal errors give two stable codes without parsing messages.

* **Negative / Trade-offs:**
  - Adding an error means remembering to add a table row; the completeness test turns a forgotten row into a failing test.
  - The shape of the error response body is decided separately, in ADR 0009.

* **Alternatives Considered:**
  - **Unbalanced entries as a `LedgerError` mapped to 500:** rejected. It would break the meaning of `LedgerError` as an expected outcome.
  - **Intermediate bases such as `NotFoundError` and `ConflictError`:** not chosen. Only 404 and 409 have more than one member, and the table does the same job.
  - **An `http_status` attribute on each error:** not chosen. It would put HTTP into the domain code.
  - **Validation in the service layer too:** rejected, because it duplicates the rules and ties service functions to HTTP concepts.
  - **One reversal error with a reason field:** rejected in favor of two classes, because the causes and places of detection differ.
