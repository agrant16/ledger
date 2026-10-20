"""transactional_service_method (ledger/service/decorators.py; ADR 0005, ADR 0013).

    @transactional_service_method
    def create_account(conn, ...): ...

A service function takes the request's connection and never commits: the API layer owns the
transaction (run_in_transaction), so the connection must already have one in progress. The decorator
refuses a connection in autocommit mode before the function runs, with a LedgerInvariantError (a bug
in the caller, never a LedgerError) that names the function. It replaces the explicit
`require_transaction(conn, "name")` call at the top of each service function.

No database is needed: the check only reads the connection's `autocommit` attribute.
"""

import inspect

import pytest

from ledger.errors import LedgerError, LedgerInvariantError
from ledger.service.decorators import transactional_service_method as decorate


class FakeConnection:
    def __init__(self, autocommit: bool) -> None:
        self.autocommit = autocommit


IN_TRANSACTION = FakeConnection(autocommit=False)
AUTOCOMMIT = FakeConnection(autocommit=True)


def test_the_function_runs_when_a_transaction_is_in_progress() -> None:
    @decorate
    def work(conn, value):
        return ("ran", conn, value)

    assert work(IN_TRANSACTION, 7) == ("ran", IN_TRANSACTION, 7)


def test_arguments_and_keywords_are_passed_through() -> None:
    @decorate
    def work(conn, first, second=2, *, flag=False):
        return (first, second, flag)

    assert work(IN_TRANSACTION, 1, second=5, flag=True) == (1, 5, True)


def test_the_connection_can_be_passed_by_keyword() -> None:
    @decorate
    def work(conn, value):
        return value

    assert work(conn=IN_TRANSACTION, value=3) == 3


def test_an_autocommit_connection_is_refused_before_the_function_runs() -> None:
    calls = []

    @decorate
    def work(conn):
        calls.append("ran")

    with pytest.raises(LedgerInvariantError):
        work(AUTOCOMMIT)

    assert calls == []


def test_an_autocommit_connection_passed_by_keyword_is_refused_too() -> None:
    @decorate
    def work(conn):
        return "ran"

    with pytest.raises(LedgerInvariantError):
        work(conn=AUTOCOMMIT)


def test_the_error_is_a_bug_and_names_the_function() -> None:
    @decorate
    def create_something(conn):
        return "ran"

    with pytest.raises(LedgerInvariantError) as failure:
        create_something(AUTOCOMMIT)

    assert not isinstance(failure.value, LedgerError)
    assert "create_something" in str(failure.value)


def test_the_function_keeps_its_name_documentation_and_signature() -> None:
    def documented(conn, amount: int, *, note: str = "") -> int:
        """Does something documented."""
        return amount

    wrapped = decorate(documented)

    assert wrapped.__name__ == "documented"
    assert wrapped.__doc__ == "Does something documented."
    assert inspect.signature(wrapped) == inspect.signature(documented)


def test_an_error_raised_by_the_function_is_not_changed() -> None:
    class Boom(Exception):
        pass

    @decorate
    def work(conn):
        raise Boom

    with pytest.raises(Boom):
        work(IN_TRANSACTION)


def test_each_call_is_checked_again() -> None:
    """The connection is checked on every call, not once when the function is defined."""

    @decorate
    def work(conn):
        return "ran"

    assert work(IN_TRANSACTION) == "ran"
    with pytest.raises(LedgerInvariantError):
        work(AUTOCOMMIT)
    assert work(IN_TRANSACTION) == "ran"


# --- no usable connection: the check fails closed ---------------------------------------------


def test_a_call_with_no_connection_at_all_is_refused() -> None:
    calls = []

    @decorate
    def work(conn=None):
        calls.append("ran")

    with pytest.raises(LedgerInvariantError) as failure:
        work()

    assert not isinstance(failure.value, LedgerError)
    assert "work" in str(failure.value)
    assert calls == []


def test_a_none_connection_is_refused() -> None:
    calls = []

    @decorate
    def work(conn):
        calls.append("ran")

    with pytest.raises(LedgerInvariantError):
        work(None)
    with pytest.raises(LedgerInvariantError):
        work(conn=None)

    assert calls == []


def test_something_that_is_not_a_connection_is_refused_and_the_function_does_not_run() -> None:
    """A pool, a cursor or a plain value has no `autocommit` attribute. Passing one by mistake must
    never be treated as 'no autocommit, so fine'."""
    calls = []

    @decorate
    def work(conn):
        calls.append("ran")

    with pytest.raises((LedgerInvariantError, AttributeError)):
        work(object())

    assert calls == []
