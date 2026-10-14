"""Account flows through the whole stack (docs/design.md, API; ADRs 0011 and 0013).

These tests cross layers on purpose, so they live in e2e and not with the HTTP-contract tests in
tests/integration/api: a route, the service, the repository and the real database in one flow.

- Creating an account through the route writes the account and its zero balance row together.
- Two creates of the same name at the same moment give one 201 and one 409.
- A balance read does not wait for a row lock that a transfer in flight holds.
- The real app (ledger.main.app, with its startup) creates an account and reads its balance.
"""

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


def create(client: TestClient, name: str = "customer:alice", currency: str = "USD") -> dict:
    response = client.post("/accounts", json={"account_name": name, "currency": currency})
    assert response.status_code == 201, response.text
    return response.json()


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


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
