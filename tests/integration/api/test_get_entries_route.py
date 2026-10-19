"""GET /accounts/{account_id}/entries (docs/design.md, API table and Id bounds; ADR 0009; #88).

Contract these tests expect:

- ledger/api/rest/routers/accounts.py has the route, so the real app serves
  `GET /accounts/{account_id}/entries`. The path id is `AccountIdInPath` (plain decimal digits).
- The route does all of its database work in one `run_in_transaction(pool, work, read_only=True)`
  call. The module imports `get_entry_history` (from ledger.service.account_service) and calls it
  by that name, so a test can replace it.
- Query parameters: `limit` (default 50, 1 to 200, plain decimal digits only, so `+5`, `05` and
  `5.0` are rejected; anything outside the range is a 400 and is never clamped) and an optional
  `cursor` (base64url of the last returned entry id, see tests/unit/api/test_cursor.py).
- 200 with {"entries": [...], "next_cursor": str | None}, and no other member (no total count).
  Each entry has exactly: id, transaction_id, account_id, amount_minor, currency, created_at (the
  owning transaction's time, ISO 8601 in UTC with a trailing Z). Entries are newest first by entry
  id. `next_cursor` is the cursor of the last returned entry when an older entry exists, and null
  on the last page.
- A bad `limit` or `cursor` is a 400 (urn:ledger:error:invalid-request) whose `errors` item names
  the `parameter` (`limit` or `cursor`). Stateless checks come first, so a bad cursor on an unknown
  account is a 400, not a 404. A bad path id is a 400 with `parameter` `account_id`.
- An unknown account is a 404 (urn:ledger:error:unknown-account); a real account with no entries
  is an empty page.
- `MAX_PAGE_SIZE` (ledger/models/entry_history_model.py) is the one place the maximum of 200 is
  written; the OpenAPI document shows it as the maximum of `limit`.
- The OpenAPI description says the entries are newest first and explains the cursor.
"""

import base64
import logging
import re
from datetime import datetime

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.router import router
from ledger.models.entry_history_model import MAX_PAGE_SIZE
from tests.db_helpers import commit_accounts, commit_transfer, commit_transfers, reset_database

CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
ENTRY_FIELDS = {"id", "transaction_id", "account_id", "amount_minor", "currency", "created_at"}


def cursor_for(text: str) -> str:
    """The base64url of the text without padding. Written out here so the tests do not depend on
    the encoder under test."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture
def app(app_pool: ConnectionPool, owner_conn: psycopg.Connection) -> FastAPI:
    """A throwaway app with the real router, the error handling and the shared test pool."""
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
    """Committed funding, alice and bob accounts (the app fixture resets the database after)."""
    return commit_accounts(app_pool)


def entry_ids(owner_conn: psycopg.Connection, account_id: int) -> list[int]:
    rows = owner_conn.execute(
        "SELECT id FROM entries WHERE account_id = %s ORDER BY id", (account_id,)
    ).fetchall()
    return [row[0] for row in rows]


# --- the body ---------------------------------------------------------------------------------


def test_the_page_has_entries_and_next_cursor_and_nothing_else(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfer(app_pool, "k1", funding.id, alice.id, 2500)

    response = client.get(f"/accounts/{alice.id}/entries")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Request-ID"]
    body = response.json()
    assert set(body) == {"entries", "next_cursor"}
    assert body["next_cursor"] is None


def test_each_entry_has_exactly_the_six_documented_fields(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 3)

    for entry in client.get(f"/accounts/{alice.id}/entries").json()["entries"]:
        assert set(entry) == ENTRY_FIELDS


def test_the_entry_values_are_the_stored_ones(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 2500)

    (entry,) = client.get(f"/accounts/{alice.id}/entries").json()["entries"]

    stored = owner_conn.execute(
        "SELECT id, amount_minor, currency FROM entries"
        " WHERE transaction_id = %s AND account_id = %s",
        (tx, alice.id),
    ).fetchone()
    assert entry["id"] == stored[0]
    assert entry["transaction_id"] == tx
    assert entry["account_id"] == alice.id
    assert entry["amount_minor"] == 2500 == stored[1]
    assert entry["currency"] == "USD" == stored[2]


def test_created_at_is_the_transactions_time_in_utc_with_a_z(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 5)

    created_at = client.get(f"/accounts/{alice.id}/entries").json()["entries"][0]["created_at"]

    assert CREATED_AT.match(created_at), created_at
    stored = owner_conn.execute("SELECT created_at FROM transactions WHERE id = %s", (tx,))
    assert datetime.fromisoformat(created_at) == stored.fetchone()[0]


def test_entries_are_newest_first_by_entry_id(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 5)

    body = client.get(f"/accounts/{alice.id}/entries").json()

    assert [e["id"] for e in body["entries"]] == sorted(
        entry_ids(owner_conn, alice.id), reverse=True
    )


def test_only_the_accounts_own_entries_are_returned_with_their_sign(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, bob = accounts
    commit_transfer(app_pool, "k1", funding.id, alice.id, 100)
    commit_transfer(app_pool, "k2", alice.id, bob.id, 30)

    alice_entries = client.get(f"/accounts/{alice.id}/entries").json()["entries"]
    funding_entries = client.get(f"/accounts/{funding.id}/entries").json()["entries"]

    assert {e["account_id"] for e in alice_entries} == {alice.id}
    assert [e["amount_minor"] for e in alice_entries] == [-30, 100]
    assert [e["amount_minor"] for e in funding_entries] == [-100]


def test_an_account_with_no_entries_is_an_empty_page(client: TestClient, accounts) -> None:
    _, alice, _ = accounts

    response = client.get(f"/accounts/{alice.id}/entries")

    assert response.status_code == 200
    assert response.json() == {"entries": [], "next_cursor": None}


def test_reading_twice_gives_the_same_body(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 3)

    url = f"/accounts/{alice.id}/entries"
    assert client.get(url).text == client.get(url).text


# --- limit ------------------------------------------------------------------------------------


def test_the_default_limit_is_50(client: TestClient, app_pool: ConnectionPool, accounts) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 55)

    body = client.get(f"/accounts/{alice.id}/entries").json()

    assert len(body["entries"]) == 50
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("limit", ["1", "7", "200"])
def test_a_limit_inside_the_range_is_honoured(
    client: TestClient, app_pool: ConnectionPool, accounts, limit: str
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 8)

    body = client.get(f"/accounts/{alice.id}/entries?limit={limit}").json()

    assert len(body["entries"]) == min(int(limit), 8)
    assert (body["next_cursor"] is None) == (int(limit) >= 8)


@pytest.mark.parametrize(
    "raw",
    [
        "0",
        "-1",
        "201",
        "1000",
        "abc",
        "",
        "1.5",
        "5.0",
        "1e2",
        "+5",
        "05",
        "%205",
        "5%20",
        "9223372036854775808",
    ],
)
def test_a_limit_that_is_not_plain_digits_in_range_is_a_400_and_never_clamped(
    client: TestClient, app_pool: ConnectionPool, accounts, raw: str
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 3)

    response = client.get(f"/accounts/{alice.id}/entries?limit={raw}")

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:ledger:error:invalid-request"
    assert body["errors"][0]["parameter"] == "limit"
    assert "entries" not in body


# --- cursor -----------------------------------------------------------------------------------


def test_next_cursor_is_the_base64url_of_the_last_returned_entry_id(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 5)

    body = client.get(f"/accounts/{alice.id}/entries?limit=2").json()

    assert body["next_cursor"] == cursor_for(str(body["entries"][-1]["id"]))
    assert "=" not in body["next_cursor"]


def test_a_cursor_returns_only_older_entries(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 5)
    newest_first = sorted(entry_ids(owner_conn, alice.id), reverse=True)

    response = client.get(
        f"/accounts/{alice.id}/entries", params={"cursor": cursor_for(str(newest_first[1]))}
    )

    assert [e["id"] for e in response.json()["entries"]] == newest_first[2:]


def test_a_cursor_past_the_oldest_entry_is_an_empty_last_page(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 3)
    oldest = min(entry_ids(owner_conn, alice.id))

    response = client.get(f"/accounts/{alice.id}/entries?cursor={cursor_for(str(oldest))}")

    assert response.status_code == 200
    assert response.json() == {"entries": [], "next_cursor": None}


def test_a_cursor_above_every_entry_gives_the_first_page(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    commit_transfers(app_pool, "k", funding.id, alice.id, 3)

    response = client.get(f"/accounts/{alice.id}/entries?cursor={cursor_for('9000000')}")

    assert len(response.json()["entries"]) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "!!!!",
        "abc=",
        "A",
        "MQ==",  # padded
        cursor_for("0"),
        cursor_for("-5"),
        cursor_for("012"),
        cursor_for("+7"),
        cursor_for(" 7"),
        cursor_for("abc"),
        cursor_for("9223372036854775808"),
    ],
)
def test_a_bad_cursor_is_a_400_naming_the_cursor_parameter(
    client: TestClient, accounts, bad: str
) -> None:
    _, alice, _ = accounts

    response = client.get(f"/accounts/{alice.id}/entries", params={"cursor": bad})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:ledger:error:invalid-request"
    assert body["errors"][0]["parameter"] == "cursor"
    if len(bad) >= 8:  # shorter strings could appear in the response by chance
        assert bad not in response.text, "the client's text must not be echoed back"


# --- the account ------------------------------------------------------------------------------


def test_an_unknown_account_is_a_404(client: TestClient) -> None:
    response = client.get("/accounts/999999/entries")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:ledger:error:unknown-account"
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"


@pytest.mark.parametrize("raw", ["abc", "0", "012", "%2B1", "1.5", "9223372036854775808"])
def test_an_account_id_that_is_not_canonical_digits_is_a_400(client: TestClient, raw: str) -> None:
    response = client.get(f"/accounts/{raw}/entries")

    assert response.status_code == 400
    assert response.json()["type"] == "urn:ledger:error:invalid-request"
    assert response.json()["errors"][0]["parameter"] == "account_id"


def test_stateless_checks_come_before_the_account_lookup(client: TestClient) -> None:
    """An unknown account with a bad limit or cursor is a 400, not a 404."""
    assert client.get("/accounts/999999/entries?limit=0").status_code == 400
    assert client.get("/accounts/999999/entries?cursor=!!!!").status_code == 400
    assert client.get("/accounts/999999/entries?limit=5").status_code == 404


# --- transactions -----------------------------------------------------------------------------


def test_the_read_runs_in_a_read_only_transaction(
    client: TestClient,
    app_pool: ConnectionPool,
    owner_conn: psycopg.Connection,
    accounts,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Replace the service read with one that tries to write: the database must refuse."""
    funding, alice, _ = accounts
    commit_transfer(app_pool, "k1", funding.id, alice.id, 5)
    caplog.set_level(logging.DEBUG)

    def writes(conn, account_id, before_id, limit):
        conn.execute("UPDATE balances SET balance_minor = 99 WHERE account_id = %s", (alice.id,))

    monkeypatch.setattr("ledger.api.rest.routers.accounts.get_entry_history", writes)

    response = client.get(f"/accounts/{alice.id}/entries")

    assert response.status_code == 500
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert logged[0].exc_info[0] is psycopg.errors.ReadOnlySqlTransaction
    row = owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (alice.id,)
    ).fetchone()
    assert row == (5,)


# --- the OpenAPI document ---------------------------------------------------------------------


def operation(client: TestClient) -> dict:
    return client.get("/openapi.json").json()["paths"]["/accounts/{account_id}/entries"]["get"]


def parameter(op: dict, name: str) -> dict:
    return next(p for p in op["parameters"] if p["name"] == name)


def test_the_endpoint_is_documented(client: TestClient) -> None:
    op = operation(client)

    text = f"{op.get('summary', '')} {op.get('description', '')}".lower()
    assert "200" in op["responses"]
    assert "newest" in text
    assert "cursor" in text
    assert "next_cursor" in text


def test_the_limit_parameter_documents_its_range_and_default(client: TestClient) -> None:
    schema = parameter(operation(client), "limit")["schema"]

    # With a pre-validator on the type, FastAPI may write the bounds as `ge` and `le`, not
    # OpenAPI's `minimum` and `maximum`; either spelling documents the range.
    assert schema["default"] == 50
    assert schema.get("minimum", schema.get("ge")) == 1
    assert schema.get("maximum", schema.get("le")) == MAX_PAGE_SIZE


def test_the_shared_maximum_page_size_is_200() -> None:
    assert MAX_PAGE_SIZE == 200
