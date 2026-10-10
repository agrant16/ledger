"""The error taxonomy in ledger/errors.py (docs/design.md, Error model).

Expected failures are LedgerError subclasses; the API layer maps each to an HTTP status. Bugs are
not LedgerErrors, so nothing can treat them as an ordinary outcome.
"""

import pytest

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


@pytest.fixture
def errors():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger import errors

    return errors


def test_ledger_error_is_an_exception(errors) -> None:
    assert issubclass(errors.LedgerError, Exception)


@pytest.mark.parametrize("name", EXPECTED_FAILURES)
def test_each_expected_failure_is_a_ledger_error(errors, name: str) -> None:
    assert issubclass(getattr(errors, name), errors.LedgerError)


def test_bugs_are_kept_out_of_the_expected_failure_hierarchy(errors) -> None:
    assert issubclass(errors.LedgerInvariantError, Exception)
    assert not issubclass(errors.LedgerInvariantError, errors.LedgerError)
    assert issubclass(errors.UnbalancedEntriesError, errors.LedgerInvariantError)
    assert not issubclass(errors.UnbalancedEntriesError, errors.LedgerError)


def test_the_two_reversal_conflicts_are_separate_errors(errors) -> None:
    """Both map to 409, but they arise in different places and mean different things to a client."""
    assert errors.AlreadyReversedError is not errors.ReversalOfReversalError
    assert not issubclass(errors.AlreadyReversedError, errors.ReversalOfReversalError)
    assert not issubclass(errors.ReversalOfReversalError, errors.AlreadyReversedError)
