"""The shared maximum page size (issue #88; docs/design.md, GET /accounts/{id}/entries).

`MAX_PAGE_SIZE` in ledger/models/entry_history_model.py is the one place the maximum of 200 is
written. `get_entry_history` uses it for its range check, so a limit of exactly MAX_PAGE_SIZE is
accepted and one more is a LedgerInvariantError (a bug, since the API guarantees the range). The
route uses the same constant for its `limit` validation (tests/integration/api/
test_get_entries_route.py checks the OpenAPI maximum). The service module imports the constant by
name (`from ledger.models.entry_history_model import MAX_PAGE_SIZE`), so the last test can replace
it; a hardcoded 200 would make that test fail.
"""

import psycopg
import pytest

from ledger.errors import LedgerInvariantError
from ledger.models.entry_history_model import MAX_PAGE_SIZE
from ledger.service.account_service import create_account, get_entry_history


@pytest.fixture
def alice_id(clean_db: None, app_conn: psycopg.Connection) -> int:
    return create_account(app_conn, "customer:alice", "USD", False).id


def test_a_limit_of_exactly_the_maximum_is_accepted(
    app_conn: psycopg.Connection, alice_id: int
) -> None:
    page = get_entry_history(app_conn, alice_id, None, MAX_PAGE_SIZE)

    assert page.entries == ()
    assert page.has_more is False


def test_a_limit_one_above_the_maximum_is_a_bug(
    app_conn: psycopg.Connection, alice_id: int
) -> None:
    with pytest.raises(LedgerInvariantError):
        get_entry_history(app_conn, alice_id, None, MAX_PAGE_SIZE + 1)


def test_the_service_follows_the_constant_not_a_hardcoded_200(
    app_conn: psycopg.Connection,
    alice_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ledger.service.account_service.MAX_PAGE_SIZE", 5)

    get_entry_history(app_conn, alice_id, None, 5)
    with pytest.raises(LedgerInvariantError):
        get_entry_history(app_conn, alice_id, None, 6)
