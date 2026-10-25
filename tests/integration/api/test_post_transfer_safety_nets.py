"""POST /transfers: failures after the key is claimed (docs/design.md, M3: "Failed transfers do not
burn the key" and "Safety nets through POST /transfers"; Correctness rules 1, 3, 4 and 5; ADR 0011,
0012, 0013).

The claim and the posting share one database transaction, so any failure rolls the claim back and
the same Idempotency-Key can be used again. Two kinds of failure are covered:

- an unexpected exception that is none of the safety nets (a RuntimeError raised after the key
  insert);
- each of the four safety nets, reached by breaking exactly one thing inside the service layer:
  unbalanced entries (`UnbalancedEntriesError`), the `balances` CHECK, the zero-sum trigger and the
  no-entries trigger. The two triggers are deferred, so they fire when the transaction commits.
  A fifth, raised by the service itself, is the stored transfer missing when it is read back after
  the claim and the post (`LedgerInvariantError`, issue #161): a bug, so a 500 and never the 404
  that `GET /transfers/{id}` gives for an unknown id.

Each one must return a generic 500 `about:blank` problem that leaks nothing from the database, log
one ERROR record with the exception, persist nothing, leave the key reusable, not be retried (the
broken function is called once) and leave the pool healthy. The pool here has a single connection,
so a connection that was not returned would make the next request time out. A normal overdraft is
the control: it is a 422, not a 500.
"""

import dataclasses
import logging
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

import ledger.service.posting_service as posting_service
from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.router import router
from ledger.errors import LedgerInvariantError, UnbalancedEntriesError
from ledger.models.posting_model import Entry
from ledger.models.transfer_model import Transfer, TransferEntry, TransferPayload
from ledger.repository import posting_repo
from tests.db_helpers import commit_accounts, commit_transfer, reset_database

KEY = "net-key"
FUNDED = 1000  # what alice holds when a test starts
NORMAL_AMOUNT = 300  # within FUNDED
OVERDRAFT_AMOUNT = FUNDED + 500  # more than alice holds

# What the generic 500 body must never contain: the database's own words.
LEAKS = ("integrity", "constraint", "trigger", "balances", "entries", "check", "boom")


@pytest.fixture
def pool(settings, owner_conn: psycopg.Connection):
    """One connection only: a request that does not give its connection back blocks the next."""
    reset_database(owner_conn)
    with ConnectionPool(settings.app_url, min_size=1, max_size=1, timeout=2, open=True) as pool:
        yield pool
    reset_database(owner_conn)


@pytest.fixture
def client(pool: ConnectionPool) -> TestClient:
    application = FastAPI()
    install_error_handling(application)
    application.include_router(router)
    application.state.pool = pool
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def accounts(pool: ConnectionPool):
    """Committed funding, alice (funded with FUNDED) and bob accounts."""
    funding, alice, bob = commit_accounts(pool)
    commit_transfer(pool, "fund-alice", funding.id, alice.id, FUNDED)
    return funding, alice, bob


def post(client: TestClient, accounts, amount_minor: int, key: str = KEY):
    _, alice, bob = accounts
    return client.post(
        "/transfers",
        json={
            "from_account_id": alice.id,
            "to_account_id": bob.id,
            "amount_minor": amount_minor,
            "currency": "USD",
        },
        headers={"Idempotency-Key": key},
    )


def stored(owner_conn: psycopg.Connection) -> tuple:
    """Everything a transfer could change: transactions, entries and every balance."""
    return (
        owner_conn.execute("SELECT count(*) FROM transactions").fetchone()[0],
        owner_conn.execute("SELECT count(*) FROM entries").fetchone()[0],
        owner_conn.execute(
            "SELECT account_id, balance_minor FROM balances ORDER BY account_id"
        ).fetchall(),
    )


# --- how each safety net is reached -------------------------------------------------------------
# Each function breaks one thing with monkeypatch and returns the list its replacement appends to
# on every call, so a test can assert the broken step ran once and was not retried.


def break_the_entries(monkeypatch: pytest.MonkeyPatch) -> list:
    """Build entries that do not sum to zero, so post_transaction raises UnbalancedEntriesError."""
    calls: list = []

    def unbalanced(payload: TransferPayload):
        calls.append(1)
        return [
            Entry(payload.from_account_id, -payload.amount_minor, payload.currency),
            Entry(payload.to_account_id, payload.amount_minor + 1, payload.currency),
        ]

    monkeypatch.setattr(posting_service, "entries_for_transfer", unbalanced)
    return calls


def break_the_balances_check(monkeypatch: pytest.MonkeyPatch) -> list:
    """Report every locked balance as 10000 higher than it is, as a stale read would, so the
    overdraft check passes and the database's CHECK on `balances` is the only thing left to refuse
    the overdraft."""
    calls: list = []
    real = posting_repo.lock_accounts

    def stale(conn: psycopg.Connection, account_ids: list[int]):
        calls.append(1)
        return {
            account_id: dataclasses.replace(locked, balance_minor=locked.balance_minor + 10_000)
            for account_id, locked in real(conn, account_ids).items()
        }

    monkeypatch.setattr(posting_repo, "lock_accounts", stale)
    return calls


def break_the_zero_sum(monkeypatch: pytest.MonkeyPatch) -> list:
    """Write one entry 1 larger than the posting checked, so the stored entries do not sum to
    zero and the deferred zero-sum trigger refuses the commit."""
    calls: list = []
    real = posting_repo.insert_entries

    def skewed(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
        calls.append(1)
        first, *rest = entries
        real(
            conn,
            transaction_id,
            [dataclasses.replace(first, amount_minor=first.amount_minor + 1), *rest],
        )

    monkeypatch.setattr(posting_repo, "insert_entries", skewed)
    return calls


def break_the_no_entries(monkeypatch: pytest.MonkeyPatch) -> list:
    """Write no entries at all, so the deferred no-entries trigger refuses the commit. The read of
    the stored transfer is stubbed too, because it would otherwise raise its own invariant error
    for a transaction without entries before the commit is reached."""
    calls: list = []

    def write_nothing(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
        calls.append(1)

    def a_transfer_with_one_entry(conn: psycopg.Connection, transfer_id: int) -> Transfer:
        # Placeholder values: the read-back only checks that the entries are not empty.
        return Transfer(
            id=transfer_id,
            reverses_transaction_id=None,
            created_at=datetime.now(UTC),
            entries=(TransferEntry(id=0, account_id=0, amount_minor=0, currency="USD"),),
        )

    monkeypatch.setattr(posting_repo, "insert_entries", write_nothing)
    # Deliberate order: claim_and_post reads the stored transfer before the commit. Stubbed, that
    # read does not raise, so the failure comes from the trigger at commit (the test asserts the
    # cause, so a reordering that let the read raise first would fail it).
    monkeypatch.setattr(posting_repo, "find_transfer", a_transfer_with_one_entry)
    return calls


def break_the_read_back(monkeypatch: pytest.MonkeyPatch) -> list:
    """Make the read of the stored transfer find nothing, as if the row just written were gone.
    The database keeps this from happening for real (the claim row exists), so the service is the
    only thing left to report it, as a bug and not as a 404."""
    calls: list = []

    def find_nothing(conn: psycopg.Connection, transfer_id: int) -> None:
        calls.append(1)

    monkeypatch.setattr(posting_repo, "find_transfer", find_nothing)
    return calls


# net -> (how to break it, amount to send, logged exception type, database cause or None). The cause
# is (psycopg error type, text its constraint name or message must contain): it proves the failure
# came from the database safety net and not from an earlier check in the service. All three
# database nets raise check_violation, so the type alone cannot tell them apart. The read-back net
# is raised by the service itself, so it has no cause: a cause would mean it was translated from
# another error (such as UnknownTransferError, a 404) and not raised as the bug it is.
NETS = {
    "unbalanced-entries": (break_the_entries, NORMAL_AMOUNT, UnbalancedEntriesError, None),
    "balances-check": (
        break_the_balances_check,
        OVERDRAFT_AMOUNT,
        LedgerInvariantError,
        (psycopg.errors.CheckViolation, "balances_protected_nonnegative"),
    ),
    "zero-sum-trigger": (
        break_the_zero_sum,
        NORMAL_AMOUNT,
        LedgerInvariantError,
        (psycopg.errors.CheckViolation, "do not sum to zero"),
    ),
    "no-entries-trigger": (
        break_the_no_entries,
        NORMAL_AMOUNT,
        LedgerInvariantError,
        (psycopg.errors.CheckViolation, "at least one record in entries"),
    ),
    "missing-transfer-after-post": (
        break_the_read_back,
        NORMAL_AMOUNT,
        LedgerInvariantError,
        None,
    ),
}


def assert_a_generic_500(
    response,
    caplog: pytest.LogCaptureFixture,
    error_type: type,
    cause: tuple[type, str] | None = None,
) -> None:
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert body["instance"] == f"urn:uuid:{response.headers['X-Request-ID']}"
    assert not any(word in response.text.lower() for word in LEAKS)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    logged = errors[0].exc_info[1]
    assert isinstance(logged, error_type)
    if cause is None:
        assert logged.__cause__ is None
    else:
        cause_type, needle = cause
        assert isinstance(logged.__cause__, cause_type)
        diag = logged.__cause__.diag
        assert needle in f"{diag.constraint_name} {diag.message_primary}"
    assert errors[0].request_id == response.headers["X-Request-ID"]


# --- an unexpected failure after the key insert -------------------------------------------------


def test_an_unexpected_exception_after_the_key_insert_does_not_burn_the_key(
    client: TestClient,
    owner_conn: psycopg.Connection,
    accounts,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    before = stored(owner_conn)
    claim_was_inserted: list[bool] = []

    def fail_after_the_claim(conn: psycopg.Connection, transaction_id: int, entries) -> None:
        claim_was_inserted.append(
            conn.execute(
                "SELECT count(*) FROM transactions WHERE idempotency_key = %s", (KEY,)
            ).fetchone()[0]
            == 1
        )
        raise RuntimeError("boom")

    monkeypatch.setattr(posting_repo, "insert_entries", fail_after_the_claim)

    failed = post(client, accounts, NORMAL_AMOUNT)

    assert claim_was_inserted == [True]  # the failure really came after the key insert
    assert_a_generic_500(failed, caplog, RuntimeError)
    assert stored(owner_conn) == before

    monkeypatch.undo()
    retried = post(client, accounts, NORMAL_AMOUNT)

    assert retried.status_code == 201
    assert stored(owner_conn)[0] == before[0] + 1


# --- the four safety nets -----------------------------------------------------------------------


@pytest.mark.parametrize("net", NETS)
def test_a_safety_net_is_a_logged_generic_500_that_persists_nothing(
    client: TestClient,
    owner_conn: psycopg.Connection,
    accounts,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    net: str,
) -> None:
    break_it, amount, error_type, cause = NETS[net]
    caplog.set_level(logging.DEBUG)
    before = stored(owner_conn)
    calls = break_it(monkeypatch)

    response = post(client, accounts, amount)

    assert_a_generic_500(response, caplog, error_type, cause)
    assert stored(owner_conn) == before
    assert calls == [1]  # the broken step ran once: nothing retried the request


@pytest.mark.parametrize("net", NETS)
def test_the_key_and_the_pool_are_usable_after_a_safety_net(
    client: TestClient,
    owner_conn: psycopg.Connection,
    accounts,
    monkeypatch: pytest.MonkeyPatch,
    net: str,
) -> None:
    break_it, amount, _, _ = NETS[net]
    before = stored(owner_conn)
    break_it(monkeypatch)
    assert post(client, accounts, amount).status_code == 500
    monkeypatch.undo()

    # The pool has one connection: this only works if the failed request gave it back.
    retried = post(client, accounts, NORMAL_AMOUNT)

    assert retried.status_code == 201  # the key was not burned (a burned key would be a 409)
    transfers, entries, balances = stored(owner_conn)
    assert (transfers, entries) == (before[0] + 1, before[1] + 2)
    assert dict(balances)[accounts[1].id] == FUNDED - NORMAL_AMOUNT
    assert dict(balances)[accounts[2].id] == NORMAL_AMOUNT


def test_the_control_a_normal_overdraft_is_a_422_not_a_500(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    before = stored(owner_conn)

    response = post(client, accounts, OVERDRAFT_AMOUNT)

    assert response.status_code == 422
    assert response.json()["type"] == "urn:ledger:error:insufficient-funds"
    assert stored(owner_conn) == before
    assert post(client, accounts, NORMAL_AMOUNT).status_code == 201


def test_every_net_is_reached_by_a_request_that_is_valid_apart_from_the_break(
    client: TestClient, owner_conn: psycopg.Connection, accounts
) -> None:
    """Guards the matrix itself: without the break, the amounts the nets use give 201 or 422, so a
    500 above can only come from the break."""
    outcomes: dict[int, int] = {}
    for amount in {amount for _, amount, _, _ in NETS.values()}:
        outcomes[amount] = post(client, accounts, amount, key=f"plain-{amount}").status_code

    assert outcomes == {NORMAL_AMOUNT: 201, OVERDRAFT_AMOUNT: 422}
