"""A commit-time database safety net, reached through a route (docs/design.md, Error behavior;
ADR 0011).

The deferred constraint triggers fire when the database transaction commits. Because the route
commits inside run_in_transaction, that failure still happens inside the handler, and the PR 1
catch-all turns it into a logged 500. These tests prove it through the HTTP contract without any
transfer code: a throwaway route inserts a `transactions` row with no entries, which the no-entries
trigger refuses at commit.
"""

import logging

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.api.rest.error_handlers import install_error_handling
from ledger.errors import LedgerInvariantError
from ledger.infra.db import run_in_transaction
from tests.db_helpers import VALID_HASH, reset_database

LONE_TRANSACTION = "INSERT INTO transactions (idempotency_key, request_hash) VALUES ('k', %s)"


@pytest.fixture
def client(settings, owner_conn: psycopg.Connection):
    reset_database(owner_conn)
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=2, open=True) as pool:
        app = FastAPI()
        install_error_handling(app)
        app.state.pool = pool

        @app.post("/orphan-claim")
        def orphan_claim(request: Request) -> dict:
            run_in_transaction(
                request.app.state.pool, lambda conn: conn.execute(LONE_TRANSACTION, (VALID_HASH,))
            )
            return {"ok": True}

        @app.get("/ok")
        def ok(request: Request) -> dict:
            return {
                "one": run_in_transaction(
                    request.app.state.pool, lambda conn: conn.execute("SELECT 1").fetchone()[0]
                )
            }

        yield TestClient(app, raise_server_exceptions=False)
    reset_database(owner_conn)


def test_a_failure_at_commit_is_a_logged_500_and_nothing_is_kept(
    client: TestClient, owner_conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client.post("/orphan-claim")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"
    # The trigger's own message ("Transaction integrity violated: ... at least one record in
    # entries") must not reach the client.
    assert "integrity" not in response.text.lower()
    assert "entries" not in response.text.lower()

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    # The commit-time failure reaches the handler as a bug (ADR 0013): a LedgerInvariantError,
    # not a LedgerError, with the psycopg error kept as its cause so the log still shows it.
    assert errors[0].exc_info[0] is LedgerInvariantError
    assert isinstance(errors[0].exc_info[1].__cause__, psycopg.errors.CheckViolation)
    assert errors[0].request_id == response.headers["X-Request-ID"]

    row = owner_conn.execute("SELECT count(*) FROM transactions").fetchone()
    assert row == (0,)


def test_the_pool_is_healthy_after_the_failure(client: TestClient) -> None:
    """The pool has one connection. A request that fails at commit must give it back."""
    assert client.post("/orphan-claim").status_code == 500
    assert client.post("/orphan-claim").status_code == 500

    response = client.get("/ok")

    assert response.status_code == 200
    assert response.json() == {"one": 1}
