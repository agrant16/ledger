"""The error taxonomy in ledger/errors.py (docs/design.md, Error model).

Expected failures are LedgerError subclasses; the API layer maps each to an HTTP status. Bugs are
not LedgerErrors, so nothing can treat them as an ordinary outcome.
"""

import pytest

from ledger import errors

EXPECTED_FAILURES = [
    "InvalidRequestError",
    "UnknownAccountError",
    "UnknownTransferError",
    "CurrencyMismatchError",
    "InsufficientFundsError",
    "IdempotencyConflictError",
    "AlreadyReversedError",
    "ReversalOfReversalError",
    "DuplicateAccountNameError",
    "ServiceBusyError",
]


def test_ledger_error_is_an_exception() -> None:
    assert issubclass(errors.LedgerError, Exception)


@pytest.mark.parametrize("name", EXPECTED_FAILURES)
def test_each_expected_failure_is_a_ledger_error(name: str) -> None:
    assert issubclass(getattr(errors, name), errors.LedgerError)


def test_bugs_are_kept_out_of_the_expected_failure_hierarchy() -> None:
    assert issubclass(errors.LedgerInvariantError, Exception)
    assert not issubclass(errors.LedgerInvariantError, errors.LedgerError)
    assert issubclass(errors.UnbalancedEntriesError, errors.LedgerInvariantError)
    assert not issubclass(errors.UnbalancedEntriesError, errors.LedgerError)


def test_the_two_reversal_conflicts_are_separate_errors() -> None:
    """Both map to 409, but they arise in different places and mean different things to a client."""
    assert errors.AlreadyReversedError is not errors.ReversalOfReversalError
    assert not issubclass(errors.AlreadyReversedError, errors.ReversalOfReversalError)
    assert not issubclass(errors.ReversalOfReversalError, errors.AlreadyReversedError)
