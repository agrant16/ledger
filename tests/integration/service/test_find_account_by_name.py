"""find_account_by_name (ledger/service/account_service.py; issue #62).

    find_account_by_name(conn, account_name) -> Account | None

A thin service wrapper over `account_repo.find_account_by_name`, used by the seed script to learn
whether a funding account already exists and whether it is the kind the script needs. An unknown
name is `None`, not an error: for the seed script "not there yet" is the normal case. Like every
service function it takes the request's connection and refuses an autocommit one. The SQL itself is
tested in tests/integration/db/test_account_repo.py.
"""

import psycopg
import pytest

from ledger.errors import LedgerInvariantError
from ledger.infra.config import Settings
from ledger.models.account_model import Account
from ledger.service.account_service import create_account, find_account_by_name


def test_returns_the_account_with_all_its_fields(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    created = create_account(app_conn, "funding:USD", "USD", True)

    found = find_account_by_name(app_conn, "funding:USD")

    assert isinstance(found, Account)
    assert found == created


def test_returns_none_for_an_unknown_name(clean_db: None, app_conn: psycopg.Connection) -> None:
    create_account(app_conn, "funding:USD", "USD", True)

    assert find_account_by_name(app_conn, "funding:EUR") is None


def test_names_are_matched_exactly(clean_db: None, app_conn: psycopg.Connection) -> None:
    """Names are unique, exact and case-sensitive (docs/design.md, Account request)."""
    create_account(app_conn, "funding:USD", "USD", True)

    assert find_account_by_name(app_conn, "Funding:USD") is None
    assert find_account_by_name(app_conn, "funding:USD ") is None


def test_it_refuses_an_autocommit_connection(clean_db: None, settings: Settings) -> None:
    with psycopg.connect(settings.app_url, autocommit=True) as conn:
        with pytest.raises(LedgerInvariantError):
            find_account_by_name(conn, "funding:USD")
