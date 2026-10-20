"""posting_repo error translation, with a connection that fails on demand (ADR 0013).

The functions that write (`insert_claim`, `insert_entries`, `apply_balance_deltas`) have no expected
constraint outcome: a violated constraint means our own code is wrong. Every integrity error they
meet reaches the caller as a LedgerInvariantError (never a LedgerError) with the psycopg error as
its cause, and anything that is not an integrity error passes through untouched. A real database
can raise the common ones (see tests/integration/db); this file reaches all of them, with no
database, including a unique violation that names no constraint.

What the functions do with real rows (including which accounts apply_balance_deltas reports as
updated) is tested against the real database, so these tests do not depend on how a statement is
run.
"""

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError
from ledger.models.posting_model import Entry
from ledger.repository import posting_repo as repo
from tests.db_helpers import VALID_HASH
from tests.unit.repository.fakes import FakeConnection

WRITES = {
    "insert_claim": lambda repo, conn: repo.insert_claim(conn, "key-1", VALID_HASH, None),
    "insert_entries": lambda repo, conn: repo.insert_entries(conn, 1, [Entry(1, 5, "USD")]),
    "apply_balance_deltas": lambda repo, conn: repo.apply_balance_deltas(conn, {1: 5}),
}


@pytest.mark.parametrize("function", list(WRITES))
@pytest.mark.parametrize(
    "error_class",
    [
        psycopg.errors.CheckViolation,
        psycopg.errors.NotNullViolation,
        psycopg.errors.ForeignKeyViolation,
        psycopg.errors.UniqueViolation,
        psycopg.errors.ExclusionViolation,
    ],
)
def test_every_integrity_error_is_a_bug_with_its_cause_kept(function: str, error_class) -> None:
    failure = error_class("probe")

    with pytest.raises(LedgerInvariantError) as caught:
        WRITES[function](repo, FakeConnection(failure))

    assert not isinstance(caught.value, LedgerError)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("function", list(WRITES))
@pytest.mark.parametrize(
    "error_class", [psycopg.errors.SyntaxError, psycopg.errors.LockNotAvailable]
)
def test_an_error_that_is_not_an_integrity_error_is_not_translated(
    function: str, error_class
) -> None:
    failure = error_class("probe")

    with pytest.raises(error_class) as caught:
        WRITES[function](repo, FakeConnection(failure))

    assert caught.value is failure
