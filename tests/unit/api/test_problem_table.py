"""The API layer's error table (docs/design.md, Error model; ADR 0007).

The domain code stays free of HTTP: the API layer owns one table from each LedgerError subclass to
its status, its RFC 9457 `type` and its `title`, resolved through the class hierarchy. A new
LedgerError with no row must fail a test and not quietly become a 500.

Contract these tests expect from ledger/api/rest/problems.py:

    problem_for(error_class) -> ProblemSpec     # attributes: status, type, title
    raises LookupError for a class that is not a LedgerError or has no row.

The `type` strings are stored in the table, not derived from class names, so renaming a class
cannot change the public API.
"""

import re

import pytest

from ledger.api.rest import problems as table
from ledger.errors import (
    InsufficientFundsError,
    LedgerError,
    LedgerInvariantError,
    UnbalancedEntriesError,
)
from tests.unit.api.api_cases import EXPECTED, URN_PREFIX


def domain_ledger_errors() -> set[type[LedgerError]]:
    """Every LedgerError subclass defined in ledger.errors, found through the class hierarchy."""
    found: set[type[LedgerError]] = set()
    pending = [LedgerError]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass.__module__ == "ledger.errors":
                found.add(subclass)
            pending.append(subclass)
    return found


def test_the_expected_table_lists_every_ledger_error() -> None:
    """If this fails, a LedgerError was added or removed: update EXPECTED in
    tests/unit/api/api_cases.py and the API table together."""
    assert set(EXPECTED) == domain_ledger_errors()


def test_every_ledger_error_has_a_row() -> None:
    """The completeness test from the design: no LedgerError may be left without a status."""
    for error in domain_ledger_errors():
        table.problem_for(error)


@pytest.mark.parametrize(
    "error", sorted(EXPECTED, key=lambda e: e.__name__), ids=lambda e: e.__name__
)
def test_each_error_maps_to_its_status_type_and_title(error) -> None:
    status, type_, title = EXPECTED[error]
    spec = table.problem_for(error)
    assert (spec.status, spec.type, spec.title) == (status, type_, title)


def test_types_are_unique_stable_names_in_the_ledger_urn_namespace() -> None:
    types = [table.problem_for(e).type for e in domain_ledger_errors()]
    assert len(types) == len(set(types))
    for type_ in types:
        assert re.fullmatch(re.escape(URN_PREFIX) + r"[a-z]+(-[a-z]+)*", type_), type_


def test_titles_are_unique_and_not_empty() -> None:
    titles = [table.problem_for(e).title for e in domain_ledger_errors()]
    assert all(titles)
    assert len(titles) == len(set(titles))


def test_only_insufficient_funds_uses_422() -> None:
    """422 is reserved for insufficient funds; validation errors are 400."""
    statuses = {e: table.problem_for(e).status for e in domain_ledger_errors()}
    assert [e for e, s in statuses.items() if s == 422] == [InsufficientFundsError]


def test_a_subclass_resolves_through_the_hierarchy() -> None:
    class FundsOnAHolidayError(InsufficientFundsError):
        pass

    assert table.problem_for(FundsOnAHolidayError) == table.problem_for(InsufficientFundsError)


def test_an_unmapped_ledger_error_is_a_lookup_error_not_a_guess() -> None:
    class BrandNewError(LedgerError):
        pass

    with pytest.raises(LookupError):
        table.problem_for(BrandNewError)
    with pytest.raises(LookupError):
        table.problem_for(LedgerError)


@pytest.mark.parametrize("bug", [LedgerInvariantError, UnbalancedEntriesError, KeyError])
def test_bugs_have_no_row(bug) -> None:
    """Bugs are not LedgerErrors: they take the logged-500 path, never a table status."""
    with pytest.raises(LookupError):
        table.problem_for(bug)
