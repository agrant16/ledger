"""POST /transfers (docs/design.md, API, Transfer response, Error behavior; ADR 0011, 0012, 0013).

Contract these tests expect (the HTTP contract only; the business rules and the order of the
database checks are tested on `claim_and_post` in tests/integration/service/test_claim_and_post.py):

- ledger/api/rest/routers/transfers.py has `POST ""` on its existing router, so the real app serves
  `POST /transfers`. It takes the `TransferRequest` body, the `Idempotency-Key` header and the pool
  from `get_pool`, hashes the request, and does all of its database work in one
  `run_in_transaction(pool, lambda conn: claim_and_post(conn, ...))` call.
- 201 with the same body `GET /transfers/{id}` returns, for a new request and for a replay alike.
- `Idempotency-Key` is required and is 1 to 255 characters without a NUL: anything else is a 400
  whose `errors` item has `parameter` "idempotency-key" (the lowercase name FastAPI reports;
  header names are case-insensitive, RFC 9110). Header and body problems are reported
  together, and a bad request is rejected before the key is claimed, so the key stays free.
- The same key with a different request is a 409 `idempotency-conflict` that repeats neither the
  key nor the hashes. Domain failures use their usual statuses: 404 unknown-account, 400
  currency-mismatch, 422 insufficient-funds. A failed request stores nothing and the key is free.
- The OpenAPI description of the operation documents the header, the replay and the 201.
"""

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from tests.db_helpers import commit_accounts, commit_transfer, reset_database


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
    """Committed funding, alice (funded with 1000) and bob accounts."""
    funding, alice, bob = commit_accounts(app_pool)
    commit_transfer(app_pool, "fund-alice", funding.id, alice.id, 1000)
    return funding, alice, bob


def body_for(accounts, **overrides) -> dict:
    _, alice, bob = accounts
    body = {
        "from_account_id": alice.id,
        "to_account_id": bob.id,
        "amount_minor": 300,
        "currency": "USD",
    }
    return body | overrides


def post(client: TestClient, accounts, key: str | None = "k1", **overrides):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/transfers", json=body_for(accounts, **overrides), headers=headers)


def transaction_count(owner_conn: psycopg.Connection) -> int:
    return owner_conn.execute("SELECT count(*) FROM transactions").fetchone()[0]


def balance(owner_conn: psycopg.Connection, account_id: int) -> int:
    return owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()[0]


# --- a new request -----------------------------------------------------------------------------


def test_a_transfer_is_created_and_returned_with_its_entries(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    _, alice, bob = accounts

    response = post(client, accounts)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert response.headers["X-Request-ID"]
    body = response.json()
    assert set(body) == {"id", "reverses_transaction_id", "created_at", "entries"}
    assert body["reverses_transaction_id"] is None
    assert [(e["account_id"], e["amount_minor"], e["currency"]) for e in body["entries"]] == [
        (alice.id, -300, "USD"),
        (bob.id, 300, "USD"),
    ]
    assert balance(owner_conn, alice.id) == 700
    assert balance(owner_conn, bob.id) == 300


def test_the_response_is_the_body_a_later_get_returns(client: TestClient, accounts) -> None:
    created = post(client, accounts)

    assert client.get(f"/transfers/{created.json()['id']}").text == created.text


# --- replay ------------------------------------------------------------------------------------


def test_a_replay_is_a_201_with_the_identical_body_and_moves_nothing(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    _, alice, bob = accounts
    first = post(client, accounts)
    transactions = transaction_count(owner_conn)

    again = post(client, accounts)

    assert again.status_code == 201
    assert again.text == first.text
    assert transaction_count(owner_conn) == transactions
    assert balance(owner_conn, alice.id) == 700
    assert balance(owner_conn, bob.id) == 300


def test_a_replay_is_the_original_answer_even_after_the_funds_are_gone(
    client: TestClient, accounts
) -> None:
    first = post(client, accounts, amount_minor=1000)

    again = post(client, accounts, amount_minor=1000)

    assert again.status_code == 201
    assert again.text == first.text


# --- the same key with a different request -----------------------------------------------------


@pytest.mark.parametrize("change", ["amount_minor", "to_account_id", "currency"])
def test_the_same_key_with_a_different_body_is_a_409_and_moves_nothing(
    client: TestClient, owner_conn: psycopg.Connection, accounts, change: str
) -> None:
    funding, alice, _ = accounts
    post(client, accounts, key="secret-key-1")
    transactions = transaction_count(owner_conn)
    different = {
        "amount_minor": 301,
        "to_account_id": funding.id,
        "currency": "EUR",
    }[change]

    response = post(client, accounts, key="secret-key-1", **{change: different})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:ledger:error:idempotency-conflict"
    assert "secret-key-1" not in response.text
    assert transaction_count(owner_conn) == transactions
    assert balance(owner_conn, alice.id) == 700


# --- the Idempotency-Key header ----------------------------------------------------------------


def test_a_missing_key_is_a_400_naming_the_header(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    transactions = transaction_count(owner_conn)

    response = post(client, accounts, key=None)

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:ledger:error:invalid-request"
    assert [e["parameter"] for e in response.json()["errors"]] == ["idempotency-key"]
    assert transaction_count(owner_conn) == transactions


@pytest.mark.parametrize("key", ["", "k" * 256, "a\x00b"], ids=["empty", "256-chars", "nul"])
def test_an_invalid_key_is_a_400_naming_the_header(
    client: TestClient, owner_conn: psycopg.Connection, accounts, key: str
) -> None:
    transactions = transaction_count(owner_conn)

    response = post(client, accounts, key=key)

    assert response.status_code == 400
    assert [e["parameter"] for e in response.json()["errors"]] == ["idempotency-key"]
    assert transaction_count(owner_conn) == transactions


@pytest.mark.parametrize("key", ["k", "k" * 255], ids=["1-char", "255-chars"])
def test_the_key_length_limits_are_inclusive(client: TestClient, accounts, key: str) -> None:
    assert post(client, accounts, key=key).status_code == 201


# --- the stateless checks come first -----------------------------------------------------------


def test_header_and_body_problems_are_reported_together(client: TestClient, accounts) -> None:
    response = post(client, accounts, key=None, amount_minor=0)

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert {e.get("parameter") for e in errors} >= {"idempotency-key"}
    assert {e.get("pointer") for e in errors} >= {"/amount_minor"}


def test_an_invalid_body_does_not_claim_the_key(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    """A 400 for the body comes before the claim, so the key is still free for a valid request."""
    transactions = transaction_count(owner_conn)

    bad = post(client, accounts, key="k1", amount_minor=0)
    good = post(client, accounts, key="k1")

    assert bad.status_code == 400
    assert good.status_code == 201
    assert transaction_count(owner_conn) == transactions + 1


def test_a_stateless_error_beats_a_key_conflict(client: TestClient, accounts) -> None:
    """Step 1 before step 2: an invalid body with a used key is a 400, not a 409."""
    post(client, accounts, key="k1")

    response = post(client, accounts, key="k1", amount_minor=0)

    assert response.status_code == 400


def test_a_transfer_to_the_same_account_is_a_400(client: TestClient, accounts) -> None:
    _, alice, _ = accounts

    response = post(client, accounts, to_account_id=alice.id)

    assert response.status_code == 400


# --- domain failures ---------------------------------------------------------------------------


def test_an_unknown_account_is_a_404_and_the_key_stays_free(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    _, _, bob = accounts
    transactions = transaction_count(owner_conn)

    response = post(client, accounts, to_account_id=bob.id + 1000)

    assert response.status_code == 404
    assert response.json()["type"] == "urn:ledger:error:unknown-account"
    assert transaction_count(owner_conn) == transactions


def test_a_currency_mismatch_is_a_400(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    transactions = transaction_count(owner_conn)

    response = post(client, accounts, currency="EUR")

    assert response.status_code == 400
    assert response.json()["type"] == "urn:ledger:error:currency-mismatch"
    assert transaction_count(owner_conn) == transactions


def test_insufficient_funds_is_a_422_with_the_documented_members(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    _, alice, _ = accounts
    transactions = transaction_count(owner_conn)

    response = post(client, accounts, amount_minor=1500)

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "urn:ledger:error:insufficient-funds"
    assert body["account_id"] == alice.id
    assert body["balance_minor"] == 1000
    assert body["net_change_minor"] == -1500
    assert transaction_count(owner_conn) == transactions
    assert balance(owner_conn, alice.id) == 1000


def test_a_request_that_failed_for_funds_succeeds_with_the_same_key_once_funded(
    client: TestClient, app_pool: ConnectionPool, accounts
) -> None:
    funding, alice, _ = accounts
    failed = post(client, accounts, key="k1", amount_minor=1500)
    commit_transfer(app_pool, "top-up", funding.id, alice.id, 1000)

    retried = post(client, accounts, key="k1", amount_minor=1500)

    assert failed.status_code == 422
    assert retried.status_code == 201
    assert [e["amount_minor"] for e in retried.json()["entries"]] == [-1500, 1500]


# --- OpenAPI -----------------------------------------------------------------------------------


def test_the_operation_is_documented(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/transfers"]["post"]

    assert operation["summary"]
    description = operation["description"]
    assert "Idempotency-Key" in description
    assert "replay" in description.lower()
    assert "201" in operation["responses"]
    header = next(p for p in operation["parameters"] if p["name"] == "idempotency-key")
    assert header["in"] == "header"
    assert header["required"] is True
