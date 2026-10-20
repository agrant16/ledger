"""account_repo error translation, with a connection that fails on demand (ADR 0013).

A real database can only produce one unique violation from `insert_account` (the account name), so
the other branches are reached here with a fake connection whose cursor raises the error the test
asks for. No database is involved.

The rule under test: the duplicate name is the one expected outcome (DuplicateAccountNameError, a
LedgerError); every other integrity error is a bug (LedgerInvariantError, not a LedgerError) with
the psycopg error as its cause. Nothing is swallowed, and nothing raw escapes.
"""

import psycopg
import pytest

from ledger.errors import DuplicateAccountNameError, LedgerError, LedgerInvariantError
from ledger.repository import account_repo as repo
from tests.unit.repository.fakes import FakeConnection


class UniqueViolationOn(psycopg.errors.UniqueViolation):
    """A unique violation that reports the constraint name it is given, as the server's would."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__("probe")
        self.constraint = constraint

    @property
    def diag(self):
        return type("Diagnostic", (), {"constraint_name": self.constraint})()


def insert(failure: Exception):
    return repo.insert_account(FakeConnection(failure), "customer:alice", "USD", False)


def test_the_duplicate_account_name_is_the_one_expected_outcome() -> None:
    failure = UniqueViolationOn("accounts_account_name_key")

    with pytest.raises(DuplicateAccountNameError) as caught:
        insert(failure)

    assert isinstance(caught.value, LedgerError)
    assert caught.value.account_name == "customer:alice"
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("constraint", ["some_other_unique_key", None])
def test_any_other_unique_violation_is_a_bug_and_is_not_swallowed(constraint) -> None:
    failure = UniqueViolationOn(constraint)

    with pytest.raises(LedgerInvariantError) as caught:
        insert(failure)

    assert not isinstance(caught.value, LedgerError)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "error_class",
    [
        psycopg.errors.CheckViolation,
        psycopg.errors.NotNullViolation,
        psycopg.errors.ForeignKeyViolation,
        psycopg.errors.ExclusionViolation,
    ],
)
def test_every_other_integrity_error_is_a_bug_with_its_cause_kept(error_class) -> None:
    failure = error_class("probe")

    with pytest.raises(LedgerInvariantError) as caught:
        insert(failure)

    assert not isinstance(caught.value, LedgerError)
    assert caught.value.__cause__ is failure


def test_an_error_that_is_not_an_integrity_error_is_not_translated() -> None:
    with pytest.raises(psycopg.errors.SyntaxError):
        insert(psycopg.errors.SyntaxError("probe"))
