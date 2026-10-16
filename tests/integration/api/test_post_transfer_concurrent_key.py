"""POST /transfers sent at the same time (docs/design.md, M3: "Concurrent same key"; Correctness
rules 2 and 3; ADR 0012).

The key claim is `INSERT ... ON CONFLICT DO NOTHING`. A second claim of a key whose first claim is
still uncommitted waits on the first transaction, and then sees how it ended:

- the first committed: the second reads the stored claim and replays (same hash) or gets a 409
  (different hash), and moves nothing;
- the first rolled back: the key is free again, so the second claims it and posts.

The deterministic tests hold the first request's transaction open on a connection of their own, so
the second request is provably stuck behind it before the first one is resolved. The barrier tests
release two real requests together and check what must hold whichever one wins. They only sample
the interleaving: a pass does not show that the two requests really overlapped, which is what the
deterministic tests are for.

The last test is rule 3: two requests with different keys whose amounts together exceed the
balance, so exactly one may succeed.

There is no lock timeout until milestone 4, so every wait here has its own timeout: a hang must
fail the test, not the suite.
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.idempotency import request_hash
from ledger.api.rest.router import router
from ledger.api.rest.schemas.transfer_schema import TransferRequest
from ledger.service.posting_service import claim_transaction, post_transaction
from ledger.service.transfer_service import entries_for_transfer
from tests.db_helpers import commit_accounts, commit_transfer, reset_database

WAIT_SECONDS = 10
KEY = "same-key"
FUNDED = 1000  # what alice holds when a test starts
AMOUNT = 300  # an ordinary transfer, well within FUNDED
OTHER_AMOUNT = AMOUNT + 1  # the "different body" of a same-key request
TAKEOVER_AMOUNT = 400  # the second request's amount when it takes over a rolled-back key
CONTENDED_AMOUNT = 600  # two of these exceed FUNDED, one fits


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
    """Committed funding, alice (funded with FUNDED) and bob accounts."""
    funding, alice, bob = commit_accounts(app_pool)
    commit_transfer(app_pool, "fund-alice", funding.id, alice.id, FUNDED)
    return funding, alice, bob


def body_for(accounts, amount_minor: int = AMOUNT) -> dict:
    _, alice, bob = accounts
    return {
        "from_account_id": alice.id,
        "to_account_id": bob.id,
        "amount_minor": amount_minor,
        "currency": "USD",
    }


def post(client: TestClient, accounts, amount_minor: int = AMOUNT, key: str = KEY):
    return client.post(
        "/transfers", json=body_for(accounts, amount_minor), headers={"Idempotency-Key": key}
    )


def hash_of(accounts, amount_minor: int) -> str:
    """The request hash the route computes for this body, so a claim made directly in the database
    is the same claim the route would make."""
    return request_hash("POST", "/transfers", TransferRequest(**body_for(accounts, amount_minor)))


def count(owner_conn: psycopg.Connection, table: str) -> int:
    return owner_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


def balance(owner_conn: psycopg.Connection, account_id: int) -> int:
    return owner_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()[0]


def wait_until_a_backend_is_blocked(
    owner_conn: psycopg.Connection, holder_pid: int, request: Future
) -> None:
    """Wait until some session other than the holder is waiting on a transaction id.

    The blocked session is the route's own pooled connection, whose pid the test cannot see, so it
    is found through pg_locks (readable by any role; pg_stat_activity would hide the app role's
    query text from the owner role). A claim that conflicts with an uncommitted one waits on the
    other transaction's id. A wait for a balances row would look the same, but cannot happen here:
    the claim is step 2 of the request and the locks are step 4, and the holder already has the
    balances locks. Gives up early if the request has already finished, which would mean it never
    waited.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and not request.done():
        waiting = owner_conn.execute(
            "SELECT count(*) FROM pg_locks "
            "WHERE NOT granted AND locktype = 'transactionid' AND pid <> %s",
            (holder_pid,),
        ).fetchone()[0]
        if waiting:
            return
        time.sleep(0.01)
    raise AssertionError("the second request never waited behind the first one's claim")


def second_request_behind_an_open_claim(
    client: TestClient,
    owner_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    accounts,
    first_amount: int,
    second_amount: int,
    resolve: Callable[[psycopg.Connection], None],
):
    """Open a claim of KEY on its own connection (and post the transfer unless it will roll back),
    send the second request, wait until it is stuck behind the claim, then resolve the first
    transaction. Returns (first transaction id, the second request's response)."""
    _, alice, bob = accounts
    with app_pool.connection() as first, ThreadPoolExecutor(max_workers=1) as executor:
        claim = claim_transaction(first, KEY, hash_of(accounts, first_amount))
        post_transaction(
            first,
            claim.transaction_id,
            entries_for_transfer(alice.id, bob.id, first_amount, "USD"),
        )
        second = executor.submit(post, client, accounts, second_amount)
        wait_until_a_backend_is_blocked(owner_conn, first.info.backend_pid, second)
        assert not second.done()
        resolve(first)
        return claim.transaction_id, second.result(timeout=WAIT_SECONDS)


# --- one request waits for the other ------------------------------------------------------------


def test_a_same_key_same_body_request_waits_and_replays_the_committed_transfer(
    client: TestClient, owner_conn: psycopg.Connection, app_pool: ConnectionPool, accounts
) -> None:
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")
    entries = count(owner_conn, "entries")

    first_id, second = second_request_behind_an_open_claim(
        client, owner_conn, app_pool, accounts, AMOUNT, AMOUNT, lambda conn: conn.commit()
    )

    assert second.status_code == 201
    assert second.json()["id"] == first_id
    assert second.text == client.get(f"/transfers/{first_id}").text
    assert count(owner_conn, "transactions") == transfers + 1
    assert count(owner_conn, "entries") == entries + 2
    assert balance(owner_conn, alice.id) == FUNDED - AMOUNT
    assert balance(owner_conn, bob.id) == AMOUNT


def test_a_same_key_different_body_request_waits_and_is_a_409(
    client: TestClient, owner_conn: psycopg.Connection, app_pool: ConnectionPool, accounts
) -> None:
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")

    _, second = second_request_behind_an_open_claim(
        client, owner_conn, app_pool, accounts, AMOUNT, OTHER_AMOUNT, lambda conn: conn.commit()
    )

    assert second.status_code == 409
    assert second.headers["content-type"] == "application/problem+json"
    assert second.json()["type"] == "urn:ledger:error:idempotency-conflict"
    assert KEY not in second.text
    assert count(owner_conn, "transactions") == transfers + 1
    assert balance(owner_conn, alice.id) == FUNDED - AMOUNT
    assert balance(owner_conn, bob.id) == AMOUNT


def test_a_waiting_request_takes_the_key_over_when_the_first_claim_rolls_back(
    client: TestClient, owner_conn: psycopg.Connection, app_pool: ConnectionPool, accounts
) -> None:
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")

    first_id, second = second_request_behind_an_open_claim(
        client,
        owner_conn,
        app_pool,
        accounts,
        AMOUNT,
        TAKEOVER_AMOUNT,
        lambda conn: conn.rollback(),
    )

    assert second.status_code == 201
    assert second.json()["id"] != first_id
    assert count(owner_conn, "transactions") == transfers + 1
    assert balance(owner_conn, alice.id) == FUNDED - TAKEOVER_AMOUNT
    assert balance(owner_conn, bob.id) == TAKEOVER_AMOUNT


# --- two real requests released together --------------------------------------------------------


def fire_together(
    client: TestClient, accounts, amounts: tuple[int, int], keys: tuple[str, str] = (KEY, KEY)
):
    """Send one request per amount (each with its key, KEY by default), released by a barrier, and
    return both responses."""
    barrier = threading.Barrier(len(amounts))

    def send(amount_minor: int, key: str):
        barrier.wait(timeout=WAIT_SECONDS)
        return post(client, accounts, amount_minor, key)

    with ThreadPoolExecutor(max_workers=len(amounts)) as executor:
        futures = [
            executor.submit(send, amount, key) for amount, key in zip(amounts, keys, strict=True)
        ]
        return [future.result(timeout=WAIT_SECONDS) for future in futures]


@pytest.mark.parametrize("attempt", range(10))
def test_two_simultaneous_identical_requests_make_one_transfer_and_get_one_answer(
    client: TestClient, owner_conn: psycopg.Connection, accounts, attempt: int
) -> None:
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")
    entries = count(owner_conn, "entries")

    first, second = fire_together(client, accounts, (AMOUNT, AMOUNT))

    assert first.status_code == second.status_code == 201
    assert first.text == second.text
    assert count(owner_conn, "transactions") == transfers + 1
    assert count(owner_conn, "entries") == entries + 2
    assert balance(owner_conn, alice.id) == FUNDED - AMOUNT
    assert balance(owner_conn, bob.id) == AMOUNT


@pytest.mark.parametrize("attempt", range(10))
def test_two_simultaneous_requests_with_different_bodies_give_one_success_and_one_409(
    client: TestClient, owner_conn: psycopg.Connection, accounts, attempt: int
) -> None:
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")
    amounts = (AMOUNT, OTHER_AMOUNT)

    responses = fire_together(client, accounts, amounts)

    assert sorted(r.status_code for r in responses) == [201, 409]
    winner_amount = amounts[[r.status_code for r in responses].index(201)]
    loser = responses[[r.status_code for r in responses].index(409)]
    assert loser.json()["type"] == "urn:ledger:error:idempotency-conflict"
    assert count(owner_conn, "transactions") == transfers + 1
    assert balance(owner_conn, alice.id) == FUNDED - winner_amount
    assert balance(owner_conn, bob.id) == winner_amount


@pytest.mark.parametrize("attempt", range(10))
def test_two_simultaneous_requests_that_together_overdraw_give_one_201_and_one_422(
    client: TestClient, owner_conn: psycopg.Connection, accounts, attempt: int
) -> None:
    """Rule 3: different keys, so neither request is a replay and both reach the balance lock. Only
    one of them fits in FUNDED, and the other must see the first one's committed balance."""
    _, alice, bob = accounts
    transfers = count(owner_conn, "transactions")
    entries = count(owner_conn, "entries")
    assert 2 * CONTENDED_AMOUNT > FUNDED >= CONTENDED_AMOUNT

    responses = fire_together(
        client, accounts, (CONTENDED_AMOUNT, CONTENDED_AMOUNT), keys=("first-key", "second-key")
    )

    assert sorted(r.status_code for r in responses) == [201, 422]
    loser = next(r for r in responses if r.status_code == 422)
    assert loser.json()["type"] == "urn:ledger:error:insufficient-funds"
    assert count(owner_conn, "transactions") == transfers + 1
    assert count(owner_conn, "entries") == entries + 2
    assert balance(owner_conn, alice.id) == FUNDED - CONTENDED_AMOUNT
    assert balance(owner_conn, bob.id) == CONTENDED_AMOUNT
