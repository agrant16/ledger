"""Property-based test of the transfer API (docs/design.md, milestone 3; ADR 0008, ADR 0012).

LedgerMachine (test_ledger_properties.py) drives the service layer. This machine drives the HTTP
API through TestClient on a real database, so the key claim, the request hash, the replay and the
error mapping are exercised under random sequences too. Rules:

- create an account (POST /accounts), or try to create one with a name that is taken (409);
- fund an account from a funding account, send a random transfer, and spend exactly a protected
  account's balance or one cent more (201 or 422, as the model says);
- replay an accepted request: same key, same body, so the same 201 body and nothing moves;
- reuse an accepted request's key with a different amount: a 409 `idempotency-conflict`, nothing
  moves, and the response does not repeat the key;
- send a request that fails (same account, unknown account, wrong currency, or an overdraft) and
  then reuse its key: the failure stored nothing, so the key is free.

After every step, with numbers from the invariants list in docs/design.md: the shared check holds
(rules 1, 3, 4 and 6), the database and the model agree on every balance and on how many
transactions and entries exist (rule 5: only accepted requests ever move money, once each),
GET /accounts/{id}/balance matches the model for the accounts touched since the last step, and
GET /transfers/{id} returns exactly what the request returned.

The funding accounts are created through the service's create_account, because the API cannot
make an account that may go negative. Every accepted request really commits, so the deferred
triggers run too. The database is emptied when an example starts and when it ends (Hypothesis runs
__init__ once per example, so the reset lives there and not in a fixture).

The machine uses one connection at a time and sends one request at a time, so it cannot find a
race: that is the job of the concurrency tests. Reversals are added in milestone 5.
"""

import itertools
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from ledger.infra.config import load_settings
from ledger.main import create_app
from ledger.service.account_service import create_account
from tests.db_helpers import reset_database
from tests.ledger_invariants import assert_ledger_consistent

pytestmark = pytest.mark.usefixtures("migrated_database")

CURRENCIES = ("USD", "EUR")
MAX_AMOUNT = 5_000
ERROR = "urn:ledger:error:"


def other_currency(currency: str) -> str:
    return CURRENCIES[1] if currency == CURRENCIES[0] else CURRENCIES[0]


class TransferApiMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        config = load_settings()
        self.owner = psycopg.connect(config.owner_url, autocommit=True)
        try:
            reset_database(self.owner)
            # The real app: entering the client runs its lifespan, which opens the real pool.
            self.app = create_app()
            self.client = TestClient(self.app, raise_server_exceptions=False)
            self.client.__enter__()
        except BaseException:
            self.owner.close()
            raise
        self.pool = self.app.state.pool
        # account id -> (currency, allow_negative); the model of what each balance must be
        self.accounts: dict[int, tuple[str, bool]] = {}
        self.model: dict[int, int] = {}
        self.names: list[str] = []
        self.funding: dict[str, int] = {}
        # idempotency key -> (request body, the 201 response body) for every accepted request
        self.accepted: dict[str, tuple[dict, dict]] = {}
        # accounts whose balance changed since the last step; GET /balance is checked for these
        self.touched: set[int] = set()
        self.counter = itertools.count(1)

    def teardown(self) -> None:
        self.client.__exit__(None, None, None)  # runs the lifespan shutdown, which closes the pool
        reset_database(self.owner)
        self.owner.close()

    # --- helpers ------------------------------------------------------------------------------

    def committed_state(self) -> tuple[int, int, dict[int, int]]:
        """What another connection sees: entry count, transaction count, every balance."""
        entries = self.owner.execute("SELECT count(*) FROM entries").fetchone()
        transactions = self.owner.execute("SELECT count(*) FROM transactions").fetchone()
        balances = self.owner.execute("SELECT account_id, balance_minor FROM balances").fetchall()
        assert entries is not None and transactions is not None
        return entries[0], transactions[0], dict(balances)

    def new_key(self) -> str:
        return f"key-{next(self.counter)}"

    def in_currency(self, currency: str) -> list[int]:
        return sorted(a for a, (c, _) in self.accounts.items() if c == currency)

    def with_two_accounts(self) -> list[str]:
        """The currencies in which a transfer between two different accounts is possible."""
        return [c for c in CURRENCIES if len(self.in_currency(c)) >= 2]

    def draw_pair(self, data) -> tuple[int, int]:
        currency = data.draw(st.sampled_from(self.with_two_accounts()))
        first, second = data.draw(
            st.lists(
                st.sampled_from(self.in_currency(currency)), min_size=2, max_size=2, unique=True
            )
        )
        return first, second

    def protected(self) -> list[int]:
        return sorted(a for a, (_, allow_negative) in self.accounts.items() if not allow_negative)

    def body(self, sender: int, receiver: int, amount: int, currency: str | None = None) -> dict:
        return {
            "from_account_id": sender,
            "to_account_id": receiver,
            "amount_minor": amount,
            "currency": currency or self.accounts[sender][0],
        }

    def send(self, body: dict, key: str):
        return self.client.post("/transfers", json=body, headers={"Idempotency-Key": key})

    def assert_problem(self, response, status: int, error: str) -> None:
        assert response.status_code == status, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["type"] == ERROR + error

    def attempt(self, body: dict, key: str) -> None:
        """Send a transfer between known accounts. It is accepted unless the sender is protected
        and would overdraw, which is a 422 that stores nothing."""
        sender, receiver, amount = (
            body["from_account_id"],
            body["to_account_id"],
            body["amount_minor"],
        )
        overdraws = not self.accounts[sender][1] and self.model[sender] < amount
        before = self.committed_state()
        response = self.send(body, key)
        if overdraws:
            self.assert_problem(response, 422, "insufficient-funds")
            assert self.committed_state() == before, "a refused transfer changed the ledger"
            event("transfer refused: insufficient funds")
            return
        assert response.status_code == 201, response.text
        transfer = response.json()
        assert transfer["reverses_transaction_id"] is None
        assert sorted(
            (e["account_id"], e["amount_minor"], e["currency"]) for e in transfer["entries"]
        ) == sorted([(sender, -amount, body["currency"]), (receiver, amount, body["currency"])])
        self.accepted[key] = (body, transfer)
        self.model[sender] -= amount
        self.model[receiver] += amount
        self.touched |= {sender, receiver}
        event("transfer accepted")

    def unchanged(self, before) -> None:
        assert self.committed_state() == before, "a request that must move nothing changed it"

    # --- rules --------------------------------------------------------------------------------

    @initialize()
    def open_funding_accounts(self) -> None:
        for currency in CURRENCIES:
            with self.pool.connection() as conn:
                account = create_account(conn, f"funding:{currency}", currency, True)
            self.accounts[account.id] = (currency, True)
            self.model[account.id] = 0
            self.names.append(account.account_name)
            self.funding[currency] = account.id
        for currency in CURRENCIES:
            self.open_customer(currency)  # so that every transfer rule is possible from step one

    def open_customer(self, currency: str) -> None:
        name = f"customer:{next(self.counter)}"
        response = self.client.post("/accounts", json={"account_name": name, "currency": currency})
        assert response.status_code == 201, response.text
        created = response.json()
        assert (created["account_name"], created["currency"], created["allow_negative"]) == (
            name,
            currency,
            False,
        )
        self.accounts[created["id"]] = (currency, False)
        self.model[created["id"]] = 0
        self.names.append(name)
        self.touched.add(created["id"])

    @rule(currency=st.sampled_from(CURRENCIES))
    def create_an_account(self, currency: str) -> None:
        self.open_customer(currency)

    @rule(data=st.data())
    def create_an_account_with_a_taken_name(self, data) -> None:
        name = data.draw(st.sampled_from(self.names))
        currency = data.draw(st.sampled_from(CURRENCIES))
        before = self.committed_state()
        response = self.client.post("/accounts", json={"account_name": name, "currency": currency})
        self.assert_problem(response, 409, "duplicate-account-name")
        self.unchanged(before)
        event("account refused: duplicate name")

    @rule(data=st.data())
    def fund_an_account(self, data) -> None:
        """Money enters through the funding account, as it does in the real system."""
        target = data.draw(st.sampled_from(self.protected()))
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        currency = self.accounts[target][0]
        self.attempt(self.body(self.funding[currency], target, amount), self.new_key())

    @rule(data=st.data())
    def send_a_transfer(self, data) -> None:
        sender, receiver = self.draw_pair(data)
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        self.attempt(self.body(sender, receiver, amount), self.new_key())

    @rule(data=st.data())
    def spend_around_a_protected_balance(self, data) -> None:
        """Spending exactly what a protected account holds is allowed; one more is not."""
        sender = data.draw(st.sampled_from(self.protected()))
        currency = self.accounts[sender][0]
        receiver = data.draw(
            st.sampled_from([a for a in self.in_currency(currency) if a != sender])
        )
        balance = self.model[sender]
        amount = balance + (1 if balance == 0 else data.draw(st.sampled_from([0, 1])))
        self.attempt(self.body(sender, receiver, amount), self.new_key())

    @precondition(lambda self: len(self.accepted) > 0)
    @rule(data=st.data())
    def replay_an_accepted_request(self, data) -> None:
        key = data.draw(st.sampled_from(sorted(self.accepted)))
        body, original = self.accepted[key]
        before = self.committed_state()
        response = self.send(body, key)
        assert response.status_code == 201, response.text
        assert response.json() == original, "a replay returned something other than the original"
        self.unchanged(before)
        event("replay")

    @precondition(lambda self: len(self.accepted) > 0)
    @rule(data=st.data())
    def reuse_a_key_for_a_different_request(self, data) -> None:
        key = data.draw(st.sampled_from(sorted(self.accepted)))
        body, _ = self.accepted[key]
        other = {**body, "amount_minor": body["amount_minor"] % MAX_AMOUNT + 1}
        before = self.committed_state()
        response = self.send(other, key)
        self.assert_problem(response, 409, "idempotency-conflict")
        assert key not in response.text, "the conflict response repeats the key"
        self.unchanged(before)
        event("key reuse refused")

    @rule(
        data=st.data(),
        kind=st.sampled_from(["same-account", "unknown-account", "currency", "overdraft"]),
    )
    def fail_then_retry_with_the_same_key(self, data, kind: str) -> None:
        """A failed request stores nothing, so the same key works for a request that is valid."""
        sender, receiver = self.draw_pair(data)
        currency = self.accounts[sender][0]
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        key = self.new_key()
        before = self.committed_state()
        if kind == "same-account":
            bad = self.body(sender, sender, amount)
            self.assert_problem(self.send(bad, key), 400, "invalid-request")
        elif kind == "unknown-account":
            bad = self.body(sender, max(self.accounts) + data.draw(st.integers(1, 1_000)), amount)
            self.assert_problem(self.send(bad, key), 404, "unknown-account")
        elif kind == "currency":
            bad = self.body(sender, receiver, amount, other_currency(currency))
            self.assert_problem(self.send(bad, key), 400, "currency-mismatch")
        else:
            senders = self.protected()
            if not senders:
                return
            sender = data.draw(st.sampled_from(senders))
            currency = self.accounts[sender][0]
            receiver = data.draw(
                st.sampled_from([a for a in self.in_currency(currency) if a != sender])
            )
            bad = self.body(sender, receiver, self.model[sender] + 1)
            self.assert_problem(self.send(bad, key), 422, "insufficient-funds")
            self.unchanged(before)
            event(f"request failed: {kind}")
            # Fund the sender by one, then send the very same request under the same key: it is
            # evaluated fresh against the new balance and succeeds (docs/design.md, milestone 3).
            self.attempt(self.body(self.funding[currency], sender, 1), self.new_key())
            self.attempt(bad, key)
            return
        self.unchanged(before)
        event(f"request failed: {kind}")
        # The key is free: a different, valid request may use it, whatever the failed one was.
        # (The overdraft kind retries the same request, above.)
        self.attempt(self.body(sender, receiver, amount), key)

    # --- invariants, checked after every step ------------------------------------------------

    @invariant()
    def the_ledger_is_consistent(self) -> None:
        assert_ledger_consistent(self.owner)
        entries, transactions, balances = self.committed_state()
        assert balances == self.model, "the database and the model disagree"
        assert transactions == len(self.accepted), (
            "a transaction exists without an accepted request"
        )
        assert entries == 2 * len(self.accepted), "the entries do not match the accepted requests"

    @invariant()
    def the_api_agrees_with_the_model(self) -> None:
        for account_id in sorted(self.touched):
            response = self.client.get(f"/accounts/{account_id}/balance")
            assert response.status_code == 200, response.text
            expected: dict[str, Any] = {
                "account_id": account_id,
                "currency": self.accounts[account_id][0],
                "balance_minor": self.model[account_id],
            }
            assert response.json() == expected
        self.touched.clear()

    @invariant()
    def every_accepted_transfer_reads_back_as_it_was_returned(self) -> None:
        for _, original in self.accepted.values():
            response = self.client.get(f"/transfers/{original['id']}")
            assert response.status_code == 200, response.text
            assert response.json() == original


TransferApiMachine.TestCase.settings = settings(
    max_examples=50,
    stateful_step_count=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Every accepted request really commits and every step makes several HTTP calls, so this is slow.
TestTransferApiProperties = pytest.mark.timeout(120)(TransferApiMachine.TestCase)
