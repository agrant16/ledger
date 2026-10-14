"""Shared cases for the API error tests: what each LedgerError maps to (docs/design.md, Error
model, and ADR 0007) and a way to build one. The table here is the spec; the API layer's own
table in ledger/api/rest/problems.py has to agree with it."""

from collections.abc import Callable

from ledger.errors import (
    AlreadyReversedError,
    CurrencyMismatchError,
    DuplicateAccountNameError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    LedgerError,
    ReversalOfReversalError,
    ServiceBusyError,
    UnknownAccountError,
    UnknownTransferError,
)

URN_PREFIX = "urn:ledger:error:"

# class -> (status, type, title)
EXPECTED: dict[type[LedgerError], tuple[int, str, str]] = {
    InvalidRequestError: (400, "urn:ledger:error:invalid-request", "Invalid request"),
    CurrencyMismatchError: (400, "urn:ledger:error:currency-mismatch", "Currency mismatch"),
    UnknownAccountError: (404, "urn:ledger:error:unknown-account", "Unknown account"),
    UnknownTransferError: (404, "urn:ledger:error:unknown-transfer", "Unknown transfer"),
    IdempotencyConflictError: (
        409,
        "urn:ledger:error:idempotency-conflict",
        "Idempotency key reused",
    ),
    AlreadyReversedError: (409, "urn:ledger:error:already-reversed", "Transfer already reversed"),
    ReversalOfReversalError: (
        409,
        "urn:ledger:error:reversal-of-reversal",
        "A reversal cannot be reversed",
    ),
    DuplicateAccountNameError: (
        409,
        "urn:ledger:error:duplicate-account-name",
        "Duplicate account name",
    ),
    InsufficientFundsError: (422, "urn:ledger:error:insufficient-funds", "Insufficient funds"),
    ServiceBusyError: (503, "urn:ledger:error:service-busy", "Service busy"),
}

# Members a problem body carries beyond type, title, status, detail and instance.
EXTENSIONS: dict[type[LedgerError], dict[str, object]] = {
    InsufficientFundsError: {"account_id": 7, "balance_minor": 100, "amount_minor": -250},
    DuplicateAccountNameError: {"account_name": "customer:alice"},
}

STANDARD_MEMBERS = {"type", "title", "status", "detail", "instance"}

EXAMPLES: dict[type[LedgerError], Callable[[], LedgerError]] = {
    InvalidRequestError: lambda: InvalidRequestError("cursor is not valid"),
    CurrencyMismatchError: lambda: CurrencyMismatchError("account 3 holds EUR, not USD"),
    UnknownAccountError: lambda: UnknownAccountError("account 999 does not exist"),
    UnknownTransferError: lambda: UnknownTransferError("transfer 999 does not exist"),
    IdempotencyConflictError: lambda: IdempotencyConflictError("key reused with a new body"),
    AlreadyReversedError: lambda: AlreadyReversedError("transfer 5 is already reversed"),
    ReversalOfReversalError: lambda: ReversalOfReversalError("transfer 6 is a reversal"),
    DuplicateAccountNameError: lambda: DuplicateAccountNameError("customer:alice"),
    InsufficientFundsError: lambda: InsufficientFundsError(7, 100, -250),
    ServiceBusyError: lambda: ServiceBusyError("lock timeout"),
}
