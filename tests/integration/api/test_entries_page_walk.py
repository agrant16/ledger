"""Walking every page of GET /accounts/{account_id}/entries (docs/design.md, Milestone 3 and the API
table; issue #88).

Following `next_cursor` from the first page until it is null returns each of the account's entries
exactly once, newest first, whatever the relation between the number of entries and the limit:

- an exact multiple of the limit ends on a full page whose `next_cursor` is null, with no empty
  page after it (the service fetches `limit + 1` rows to know);
- a single page, and an account with no entries (one empty page);
- other accounts' entries interleaved between the account's own do not leave gaps or repeats, which
  is why the cursor is an entry id and not an offset;
- a transfer committed during the walk adds entries with higher ids than any already seen, so the
  rest of the walk neither repeats nor skips an entry that existed when it began.

Paging while writers run concurrently is milestone 4's job (tests/concurrency). These walks are
sequential. The route is tested in tests/integration/api/test_get_entries_route.py.
"""

import math

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from tests.db_helpers import commit_accounts, commit_transfers, reset_database


@pytest.fixture
def app(app_pool: ConnectionPool, owner_conn: psycopg.Connection) -> FastAPI:
    """A throwaway app with the real router, the error handling and the shared test pool."""
    from ledger.api.rest.error_handlers import install_error_handling
    from ledger.api.rest.router import router

    reset_database(owner_conn)
    application = FastAPI()
    install_error_handling(application)
    application.include_router(router)
    application.state.pool = app_pool
    yield application
    reset_database(owner_conn)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def accounts(app: FastAPI, app_pool: ConnectionPool):
    return commit_accounts(app_pool)


def get_page(client: TestClient, account_id: int, limit: int, cursor: str | None = None) -> dict:
    params = {"limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get(f"/accounts/{account_id}/entries", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client: TestClient, account_id: int, limit: int, cursor: str | None = None) -> list[dict]:
    """Every page from the given cursor to the end, as the response bodies."""
    pages = []
    while True:
        page = get_page(client, account_id, limit, cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 1000, "the walk never ended"


def walked_ids(pages: list[dict]) -> list[int]:
    return [entry["id"] for page in pages for entry in page["entries"]]


def stored_ids(owner_conn: psycopg.Connection, account_id: int) -> list[int]:
    rows = owner_conn.execute(
        "SELECT id FROM entries WHERE account_id = %s ORDER BY id DESC", (account_id,)
    ).fetchall()
    return [row[0] for row in rows]


# --- the walk ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "limit"),
    [
        (0, 5),  # no entries
        (1, 5),  # a single, short page
        (5, 5),  # exactly one full page
        (10, 5),  # an exact multiple of the limit
        (11, 5),  # one entry spills onto the last page
        (9, 5),  # a short last page
        (7, 1),  # one entry per page
        (6, 3),  # an exact multiple, other numbers
        (3, 200),  # the largest limit
    ],
)
def test_walking_every_page_returns_each_entry_exactly_once(
    client: TestClient,
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    accounts,
    count: int,
    limit: int,
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, count)

    pages = walk(client, alice.id, limit)

    ids = walked_ids(pages)
    assert ids == stored_ids(owner_conn, alice.id)  # all of them, newest first, none repeated
    assert len(set(ids)) == len(ids) == count
    assert len(pages) == max(1, math.ceil(count / limit))
    for page in pages[:-1]:
        assert len(page["entries"]) == limit
        assert page["next_cursor"] is not None
    expected_last = (count % limit or limit) if count else 0
    assert len(pages[-1]["entries"]) == expected_last
    assert pages[-1]["next_cursor"] is None


def test_an_exact_multiple_does_not_end_with_an_empty_page(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 6)

    pages = walk(client, alice.id, 3)

    assert [len(page["entries"]) for page in pages] == [3, 3]
    assert pages[-1]["next_cursor"] is None


def test_an_account_with_no_entries_is_one_empty_page(client: TestClient, accounts) -> None:
    _, alice, _ = accounts

    assert walk(client, alice.id, 10) == [{"entries": [], "next_cursor": None}]


def test_ids_strictly_decrease_across_page_boundaries(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 12)

    ids = walked_ids(walk(client, alice.id, 5))

    assert all(newer > older for newer, older in zip(ids, ids[1:], strict=False))


def test_other_accounts_entries_between_the_accounts_own_leave_no_gap_or_repeat(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    """Alice's entry ids are not consecutive when transfers to Bob are interleaved. A cursor on
    the entry id still resumes exactly after the last one seen."""
    funding, alice, bob = accounts
    for i in range(6):
        commit_transfers(app_pool, f"a{i}", funding.id, alice.id, 1)
        commit_transfers(app_pool, f"b{i}", funding.id, bob.id, 2)
    ids_of_alice = stored_ids(owner_conn, alice.id)
    assert ids_of_alice[0] - ids_of_alice[-1] > len(ids_of_alice), "the ids should have gaps"

    pages = walk(client, alice.id, 4)

    assert walked_ids(pages) == ids_of_alice
    assert {e["account_id"] for page in pages for e in page["entries"]} == {alice.id}


def test_entries_committed_during_the_walk_do_not_disturb_the_rest_of_it(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    """New entries get higher ids than anything already seen, so the pages after the first
    still return exactly the entries that existed when the walk began."""
    funding, alice, _ = accounts
    commit_transfers(app_pool, "before", funding.id, alice.id, 7)
    existing = stored_ids(owner_conn, alice.id)

    first = get_page(client, alice.id, 3)
    commit_transfers(app_pool, "during", funding.id, alice.id, 2)
    rest = walk(client, alice.id, 3, first["next_cursor"])

    ids = [e["id"] for e in first["entries"]] + walked_ids(rest)
    assert ids == existing
    assert len(stored_ids(owner_conn, alice.id)) == 9
