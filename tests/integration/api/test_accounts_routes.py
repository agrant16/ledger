"""POST /accounts and GET /accounts/{account_id}/balance (docs/design.md, API; ADR 0011).

Contract these tests expect:

- ledger/api/accounts.py has an `APIRouter` called `router` with both routes, and the real app
  (ledger.main.app) includes it.
- Each route reads the pool from `request.app.state.pool` and does all of its database work in one
  `run_in_transaction(pool, work, ...)` call. The balance route passes read_only=True.
- ledger/api/accounts.py imports `get_balance` (from ledger.service.account_service) and calls it
  by that name, so a test can replace it.
- POST /accounts: 201 with {"id", "account_name", "currency", "allow_negative": false}. The body
  cannot set allow_negative. A duplicate name is the 409 problem with `account_name` in it.
- GET balance: 200 with {"account_id", "currency", "balance_minor"}; 404 for an unknown account;
  400 for an id that is not canonical digits.
- The balance route's OpenAPI description says that it reads the cached balance and that the
  balance may not include a transfer still in flight.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from tests.db_helpers import reset_database

VALID = {"account_name": "customer:alice", "currency": "USD"}


@pytest.fixture
def app(app_pool: ConnectionPool, owner_conn: psycopg.Connection) -> FastAPI:
    """A throwaway app with the real router, the error handling and the shared test pool."""
    from ledger.api.accounts import router
    from ledger.api.errors import install_error_handling

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


def create(client: TestClient, name: str = "customer:alice", currency: str = "USD") -> dict:
    response = client.post("/accounts", json={"account_name": name, "currency": currency})
    assert response.status_code == 201, response.text
    return response.json()


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


# --- POST /accounts ---------------------------------------------------------------------------


def test_creating_an_account_returns_201_and_the_account(client: TestClient) -> None:
    response = client.post("/accounts", json=VALID)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"id", "account_name", "currency", "allow_negative"}
    assert body["id"] >= 1
    assert (body["account_name"], body["currency"], body["allow_negative"]) == (
        "customer:alice",
        "USD",
        False,
    )
    assert response.headers["X-Request-ID"]


def test_the_account_and_a_zero_balance_row_exist_afterwards(
    client: TestClient, owner_conn: psycopg.Connection
) -> None:
    account = create(client)

    account_row = owner_conn.execute(
        "SELECT account_name, currency, allow_negative FROM accounts WHERE id = %s",
        (account["id"],),
    ).fetchone()
    balance_row = owner_conn.execute(
        "SELECT allow_negative, balance_minor FROM balances WHERE account_id = %s",
        (account["id"],),
    ).fetchone()
    assert account_row == ("customer:alice", "USD", False)
    assert balance_row == (False, 0)


def test_a_duplicate_name_is_a_409_that_names_the_account(
    client: TestClient, owner_conn: psycopg.Connection
) -> None:
    create(client)

    response = client.post("/accounts", json=VALID)

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:ledger:error:duplicate-account-name"
    assert body["account_name"] == "customer:alice"
    assert (count(owner_conn, "accounts"), count(owner_conn, "balances")) == (1, 1)


def test_the_same_name_in_another_currency_is_still_a_duplicate(client: TestClient) -> None:
    create(client)

    response = client.post("/accounts", json={"account_name": "customer:alice", "currency": "EUR"})

    assert response.status_code == 409


def test_allow_negative_cannot_be_sent(client: TestClient, owner_conn: psycopg.Connection) -> None:
    response = client.post("/accounts", json={**VALID, "allow_negative": True})

    assert response.status_code == 400
    assert response.json()["errors"][0]["pointer"] == "/allow_negative"
    assert count(owner_conn, "accounts") == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_name", " padded"),
        ("account_name", ""),
        ("account_name", "x" * 101),
        ("currency", "usd"),
        ("currency", "USDD"),
    ],
)
def test_a_bad_name_or_currency_is_a_400_and_creates_nothing(
    client: TestClient, owner_conn: psycopg.Connection, field, value
) -> None:
    response = client.post("/accounts", json={**VALID, field: value})

    assert response.status_code == 400
    assert response.json()["errors"][0]["pointer"] == f"/{field}"
    assert count(owner_conn, "accounts") == 0


def test_a_name_ending_in_a_non_ascii_space_is_accepted(client: TestClient) -> None:
    """The design allows Unicode spaces; only the six ASCII whitespace characters are refused."""
    response = client.post(
        "/accounts", json={"account_name": "alice\N{NO-BREAK SPACE}", "currency": "USD"}
    )

    assert response.status_code == 201


def test_two_creates_of_the_same_name_at_once_give_one_201_and_one_409(
    app: FastAPI, owner_conn: psycopg.Connection
) -> None:
    """The unique constraint decides, so exactly one of two simultaneous creates can win."""
    start = threading.Barrier(2)

    def attempt() -> int:
        own_client = TestClient(app, raise_server_exceptions=False)
        start.wait(timeout=10)
        return own_client.post("/accounts", json=VALID).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = [f.result(timeout=30) for f in [pool.submit(attempt), pool.submit(attempt)]]

    assert sorted(statuses) == [201, 409]
    assert count(owner_conn, "accounts") == 1


# --- GET /accounts/{account_id}/balance -------------------------------------------------------


def test_a_new_account_shows_a_zero_balance(client: TestClient) -> None:
    account = create(client, currency="EUR")

    response = client.get(f"/accounts/{account['id']}/balance")

    assert response.status_code == 200
    assert response.json() == {"account_id": account["id"], "currency": "EUR", "balance_minor": 0}


def test_the_balance_is_the_cached_one(client: TestClient, owner_conn: psycopg.Connection) -> None:
    """There are no entries, so 500 can only have come from the cache."""
    account = create(client)
    owner_conn.execute(
        "UPDATE balances SET balance_minor = 500 WHERE account_id = %s", (account["id"],)
    )

    assert client.get(f"/accounts/{account['id']}/balance").json()["balance_minor"] == 500


def test_an_unknown_account_is_a_404(client: TestClient) -> None:
    response = client.get("/accounts/999999/balance")

    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "urn:ledger:error:unknown-account"
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"


@pytest.mark.parametrize("raw", ["abc", "0", "012", "%2B1", "1.5"])
def test_an_id_that_is_not_canonical_digits_is_a_400(client: TestClient, raw) -> None:
    response = client.get(f"/accounts/{raw}/balance")

    assert response.status_code == 400
    assert response.json()["errors"][0]["parameter"] == "account_id"


def test_the_read_does_not_wait_for_a_row_lock(
    client: TestClient, app_pool: ConnectionPool
) -> None:
    """A transfer in flight holds the balances row FOR UPDATE. The balance read must not wait."""
    account = create(client)
    holder = app_pool.getconn()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        holder.execute("SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE", (account["id"],))
        future = executor.submit(client.get, f"/accounts/{account['id']}/balance")
        try:
            response = future.result(timeout=5)
        except TimeoutError:
            pytest.fail("the balance read waited for a row lock")
        finally:
            holder.rollback()
            executor.shutdown(wait=True)
    finally:
        app_pool.putconn(holder)

    assert response.status_code == 200


def test_the_balance_read_runs_in_a_read_only_transaction(
    client: TestClient,
    owner_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Replace the service read with one that tries to write: the database must refuse."""
    account = create(client)
    caplog.set_level(logging.DEBUG)

    def writes(conn, account_id):
        conn.execute("UPDATE balances SET balance_minor = 99 WHERE account_id = %s", (account_id,))

    monkeypatch.setattr("ledger.api.accounts.get_balance", writes)

    response = client.get(f"/accounts/{account['id']}/balance")

    assert response.status_code == 500
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert logged[0].exc_info[0] is psycopg.errors.ReadOnlySqlTransaction
    row = owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account["id"],)
    ).fetchone()
    assert row == (0,)


# --- the OpenAPI document ---------------------------------------------------------------------


def test_the_balance_endpoint_is_documented_as_a_cached_read(client: TestClient) -> None:
    document = client.get("/openapi.json").json()

    operation = document["paths"]["/accounts/{account_id}/balance"]["get"]
    text = f"{operation.get('summary', '')} {operation.get('description', '')}".lower()
    assert "cached" in text
    assert "in flight" in text
    assert "201" in document["paths"]["/accounts"]["post"]["responses"]


# --- through the real app ---------------------------------------------------------------------


def test_the_real_app_creates_an_account_and_reads_its_balance(
    clean_db: None, owner_conn: psycopg.Connection
) -> None:
    from ledger.main import app

    try:
        with TestClient(app, raise_server_exceptions=False) as real_client:
            created = real_client.post("/accounts", json=VALID)
            assert created.status_code == 201
            balance = real_client.get(f"/accounts/{created.json()['id']}/balance")
            assert balance.status_code == 200
            assert balance.json()["balance_minor"] == 0
    finally:
        reset_database(owner_conn)
