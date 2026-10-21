"""GET /transfers/{transfer_id} (docs/design.md, API and Transfer response; ADR 0011).

Contract these tests expect:

- ledger/api/rest/routers/transfers.py has an `APIRouter` called `router` with the route, which
  the aggregate router (ledger/api/rest/router.py) includes with the prefix `/transfers`, so the
  real app serves `GET /transfers/{transfer_id}`.
- The route takes the pool from the `get_pool` dependency and does all of its database work in one
  `run_in_transaction(pool, work, read_only=True)` call.
- The module imports `get_transfer` (from ledger.service.transfer_service) and calls it by that
  name, so a test can replace it. The response is built by `TransferResponse.from_model`
  (tests/unit/api/test_transfer_schema.py).
- 200 with {"id", "reverses_transaction_id", "created_at", "entries": [{"id", "account_id",
  "amount_minor", "currency"}]}, entries in the order they were written. `created_at` is ISO 8601
  in UTC with a trailing Z. POST /transfers will return exactly this body.
- 404 (urn:ledger:error:unknown-transfer) for an unknown transfer; 400 for an id that is not
  canonical digits, with the problem's `parameter` named `transfer_id`.
- A transfer is visible only once its transaction has committed, and always with all its entries.
- The OpenAPI description says so.
"""

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
from ledger.models.posting_model import Entry
from ledger.service.posting_service import claim_transaction, post_transaction
from tests.db_helpers import commit_accounts, commit_transfer, hash_for, reset_database

CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


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


# --- the body ---------------------------------------------------------------------------------


def test_a_transfer_is_returned_with_its_entries(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 2500)

    response = client.get(f"/transfers/{tx}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Request-ID"]
    body = response.json()
    assert set(body) == {"id", "reverses_transaction_id", "created_at", "entries"}
    assert body["id"] == tx
    assert body["reverses_transaction_id"] is None
    entries = owner_conn.execute(
        "SELECT id, account_id, amount_minor, currency FROM entries"
        " WHERE transaction_id = %s ORDER BY id",
        (tx,),
    ).fetchall()
    assert body["entries"] == [
        {"id": row[0], "account_id": row[1], "amount_minor": row[2], "currency": row[3]}
        for row in entries
    ]
    assert [e["amount_minor"] for e in body["entries"]] == [-2500, 2500]
    assert [e["account_id"] for e in body["entries"]] == [funding.id, alice.id]


def test_each_entry_has_exactly_the_four_documented_fields(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 5)

    for entry in client.get(f"/transfers/{tx}").json()["entries"]:
        assert set(entry) == {"id", "account_id", "amount_minor", "currency"}


def test_created_at_is_iso_8601_in_utc_and_is_the_stored_time(
    client: TestClient, app_pool: ConnectionPool, owner_conn: psycopg.Connection, accounts
) -> None:
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 5)

    created_at = client.get(f"/transfers/{tx}").json()["created_at"]

    assert CREATED_AT.match(created_at), created_at
    stored = owner_conn.execute("SELECT created_at FROM transactions WHERE id = %s", (tx,))
    assert datetime.fromisoformat(created_at) == stored.fetchone()[0]


def test_a_reversal_shows_the_transfer_it_reverses(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    original = commit_transfer(app_pool, "k1", funding.id, alice.id, 500)
    reversal = commit_transfer(
        app_pool, "k2", alice.id, funding.id, 500, reverses_transaction_id=original
    )

    assert client.get(f"/transfers/{reversal}").json()["reverses_transaction_id"] == original
    assert client.get(f"/transfers/{original}").json()["reverses_transaction_id"] is None


def test_reading_it_twice_gives_the_same_body(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    """A replay of POST /transfers returns this body again, so it must not vary between reads."""
    funding, alice, _ = accounts
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 5)

    assert client.get(f"/transfers/{tx}").text == client.get(f"/transfers/{tx}").text


# --- errors -----------------------------------------------------------------------------------


def test_an_unknown_transfer_is_a_404(client: TestClient) -> None:
    response = client.get("/transfers/999999")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:ledger:error:unknown-transfer"
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"


@pytest.mark.parametrize("raw", ["abc", "0", "012", "%2B1", "1.5", "9223372036854775808"])
def test_an_id_that_is_not_canonical_digits_is_a_400(client: TestClient, raw: str) -> None:
    response = client.get(f"/transfers/{raw}")

    assert response.status_code == 400
    assert response.json()["type"] == "urn:ledger:error:invalid-request"
    assert response.json()["errors"][0]["parameter"] == "transfer_id"


# --- visibility and transactions --------------------------------------------------------------


def test_a_transfer_is_invisible_until_it_commits_and_then_whole(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    """Never part of a transfer: the entries and the transaction commit together."""
    funding, alice, _ = accounts
    with app_pool.connection() as writer:
        claim = claim_transaction(writer, "k1", hash_for("k1"))
        post_transaction(
            writer,
            claim.transaction_id,
            [Entry(funding.id, -5, "USD"), Entry(alice.id, 5, "USD")],
        )

        in_flight = client.get(f"/transfers/{claim.transaction_id}")

        writer.commit()
    committed = client.get(f"/transfers/{claim.transaction_id}")

    assert in_flight.status_code == 404
    assert committed.status_code == 200
    assert len(committed.json()["entries"]) == 2


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
    tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 5)
    caplog.set_level(logging.DEBUG)

    def writes(conn, transfer_id):
        conn.execute("UPDATE balances SET balance_minor = 99 WHERE account_id = %s", (alice.id,))

    monkeypatch.setattr("ledger.api.rest.routers.transfers.get_transfer", writes)

    response = client.get(f"/transfers/{tx}")

    assert response.status_code == 500
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert logged[0].exc_info[0] is psycopg.errors.ReadOnlySqlTransaction
    row = owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (alice.id,)
    ).fetchone()
    assert row == (5,)


# --- the OpenAPI document ---------------------------------------------------------------------


def test_the_endpoint_is_documented(client: TestClient) -> None:
    document = client.get("/openapi.json").json()

    operation = document["paths"]["/transfers/{transfer_id}"]["get"]
    text = f"{operation.get('summary', '')} {operation.get('description', '')}".lower()
    assert "200" in operation["responses"]
    assert "entries" in text
    assert "committed" in text
