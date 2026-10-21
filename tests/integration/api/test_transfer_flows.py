"""Transfers through the real app (ledger.main.create_app), with a real pool and database."""

import psycopg
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.main import create_app
from tests.db_helpers import commit_accounts, commit_transfer, reset_database


def test_the_real_app_returns_a_committed_transfer(
    clean_db: None, app_pool: ConnectionPool, owner_conn: psycopg.Connection
) -> None:
    try:
        funding, alice, _ = commit_accounts(app_pool)
        tx = commit_transfer(app_pool, "k1", funding.id, alice.id, 700)
        with TestClient(create_app(), raise_server_exceptions=False) as real_client:
            response = real_client.get(f"/transfers/{tx}")

        assert response.status_code == 200
        assert [e["amount_minor"] for e in response.json()["entries"]] == [-700, 700]
    finally:
        reset_database(owner_conn)
