"""Property-based test of the ledger (docs/design.md, milestone 2).

Hypothesis builds random sequences of operations against a real database: opening accounts, funding
them, and posting valid and deliberately broken sets of entries, in two currencies and with
protected and allow_negative accounts. After every step it checks the invariants from the design:

1. Every transaction's entries sum to zero per currency.
2. The per-currency total across all accounts stays at zero.
3. Every cached balance equals the sum of that account's entries.
4. No protected account is ever negative.
5. A rejected post leaves the entries, transactions and balances exactly as they were.
6. Every entry's currency matches its account's.

A small in-memory model of the balances sits next to the database. Each rule builds a post with at
most one defect, so the outcome it expects is known by construction; the model is only needed to
say whether a valid-looking post overdraws a protected account. The order in which several defects
are reported is pinned by the example tests, not here.

Each accepted post really commits, so the deferred constraint triggers run too. The database is
emptied when an example starts and when it ends. Savepoints cannot do that job: nothing inside one
is ever committed, so the triggers would never fire. Hypothesis runs the machine's __init__ once
per example, which is why the reset lives there and not in a pytest fixture.

This test uses one connection at a time, so it cannot notice a missing row lock: that is the job of
the concurrency tests in milestone 4.
"""

import itertools
from collections import defaultdict

import psycopg
import pytest
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from ledger.accounts import Account, create_account
from ledger.config import load_settings
from ledger.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    LedgerInvariantError,
    UnbalancedEntriesError,
    UnknownAccountError,
)
from ledger.posting import Entry, claim_transaction, post_transaction
from tests.db_helpers import VALID_HASH, reset_database

pytestmark = pytest.mark.usefixtures("migrated_database")

CURRENCIES = ("USD", "EUR")
MAX_AMOUNT = 5_000

nonzero_amounts = st.one_of(st.integers(-MAX_AMOUNT, -1), st.integers(1, MAX_AMOUNT))


def other_currency(currency: str) -> str:
    return CURRENCIES[1] if currency == CURRENCIES[0] else CURRENCIES[0]


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        config = load_settings()
        self.owner = psycopg.connect(config.owner_url, autocommit=True)
        self.app = psycopg.connect(config.app_url)
        reset_database(self.owner)
        self.accounts: dict[int, Account] = {}
        self.model: dict[int, int] = {}
        self.funding: dict[str, Account] = {}
        self.counter = itertools.count(1)

    def teardown(self) -> None:
        self.app.rollback()
        self.app.close()
        reset_database(self.owner)
        self.owner.close()

    # --- helpers ------------------------------------------------------------------------------

    def open(self, currency: str, allow_negative: bool) -> Account:
        name = f"{'funding' if allow_negative else 'customer'}:{next(self.counter)}"
        account = create_account(self.app, name, currency, allow_negative)
        self.app.commit()
        self.accounts[account.id] = account
        self.model[account.id] = 0
        return account

    def in_currency(self, currency: str) -> list[Account]:
        return sorted(
            (a for a in self.accounts.values() if a.currency == currency), key=lambda a: a.id
        )

    def committed_state(self) -> tuple[int, int, dict[int, int]]:
        """What another connection sees: entry count, transaction count, every balance."""
        entries = self.owner.execute("SELECT count(*) FROM entries").fetchone()
        transactions = self.owner.execute("SELECT count(*) FROM transactions").fetchone()
        balances = self.owner.execute("SELECT account_id, balance_minor FROM balances").fetchall()
        assert entries is not None and transactions is not None
        return entries[0], transactions[0], dict(balances)

    def balanced_group(self, data, currency: str) -> list[Entry]:
        """2 to 4 entries in one currency that sum to zero, over accounts that may repeat."""
        pool = self.in_currency(currency)
        size = data.draw(st.integers(2, 4), label="entries in the group")
        amounts = data.draw(st.lists(nonzero_amounts, min_size=size - 1, max_size=size - 1))
        if sum(amounts) == 0:
            amounts[0] = -amounts[0]  # the sum becomes -2 * amounts[0], which is not zero
        last = -sum(amounts)
        holders = data.draw(st.lists(st.sampled_from(pool), min_size=size, max_size=size))
        return [
            Entry(a.id, amount, currency)
            for a, amount in zip(holders, [*amounts, last], strict=True)
        ]

    def post(self, entries: list[Entry]) -> None:
        """Claim and post as one database transaction, committing only if it all worked."""
        try:
            claim = claim_transaction(self.app, f"key-{next(self.counter)}", VALID_HASH)
            transaction_id = claim.transaction_id
            post_transaction(self.app, transaction_id, entries)
            self.app.commit()
        except BaseException:
            self.app.rollback()
            raise

    def attempt(self, entries: list[Entry]) -> None:
        """Post entries that look valid. They succeed unless a protected account would overdraw."""
        net: dict[int, int] = defaultdict(int)
        for entry in entries:
            net[entry.account_id] += entry.amount_minor
        overdrawn = next(
            (
                (account_id, self.model[account_id], delta)
                for account_id, delta in sorted(net.items())
                if not self.accounts[account_id].allow_negative
                and self.model[account_id] + delta < 0
            ),
            None,
        )
        before = self.committed_state()
        try:
            self.post(entries)
        except InsufficientFundsError as failure:
            assert overdrawn == (failure.account_id, failure.balance_minor, failure.amount_minor)
            assert self.committed_state() == before, "a rejected post changed the ledger"
            event("post refused: insufficient funds")
            return
        event("post accepted")
        assert overdrawn is None, f"a post that overdraws {overdrawn} was accepted"
        for account_id, delta in net.items():
            self.model[account_id] += delta

    def reject(self, entries: list[Entry], error: type[Exception]) -> None:
        """Post entries with exactly one defect: refused with that error, and nothing changes."""
        before = self.committed_state()
        with pytest.raises(error):
            self.post(entries)
        event(f"post refused: {error.__name__}")
        assert self.committed_state() == before, "a rejected post changed the ledger"

    # --- rules --------------------------------------------------------------------------------

    @initialize()
    def open_funding_accounts(self) -> None:
        for currency in CURRENCIES:
            self.funding[currency] = self.open(currency, allow_negative=True)

    @rule(currency=st.sampled_from(CURRENCIES), allow_negative=st.booleans())
    def open_account(self, currency: str, allow_negative: bool) -> None:
        self.open(currency, allow_negative)

    @precondition(lambda self: len(self.accounts) > len(self.funding))
    @rule(data=st.data())
    def fund_an_account(self, data) -> None:
        """Money enters through the funding account, as it does in the real system."""
        targets = sorted(
            (a for a in self.accounts.values() if a.id != self.funding[a.currency].id),
            key=lambda a: a.id,
        )
        target = data.draw(st.sampled_from(targets))
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        funding = self.funding[target.currency]
        self.attempt(
            [
                Entry(funding.id, -amount, target.currency),
                Entry(target.id, amount, target.currency),
            ]
        )

    @rule(data=st.data())
    def post_balanced_entries(self, data) -> None:
        currencies = data.draw(
            st.lists(st.sampled_from(CURRENCIES), min_size=1, max_size=2, unique=True)
        )
        entries = [e for c in currencies for e in self.balanced_group(data, c)]
        self.attempt(data.draw(st.permutations(entries)))

    @precondition(lambda self: any(not a.allow_negative for a in self.accounts.values()))
    @rule(data=st.data())
    def spend_around_a_protected_balance(self, data) -> None:
        """Spending exactly what a protected account holds is allowed; one more is not."""
        protected = sorted(
            (a for a in self.accounts.values() if not a.allow_negative), key=lambda a: a.id
        )
        sender = data.draw(st.sampled_from(protected))
        others = [a for a in self.in_currency(sender.currency) if a.id != sender.id]
        receiver = data.draw(st.sampled_from(others))
        balance = self.model[sender.id]
        amount = balance + (1 if balance == 0 else data.draw(st.sampled_from([0, 1])))
        currency = sender.currency
        self.attempt([Entry(sender.id, -amount, currency), Entry(receiver.id, amount, currency)])

    @rule(data=st.data())
    def post_unbalanced_entries(self, data) -> None:
        currency = data.draw(st.sampled_from(CURRENCIES))
        entries = self.balanced_group(data, currency)
        index = data.draw(st.integers(0, len(entries) - 1))
        amount = entries[index].amount_minor
        shift = data.draw(nonzero_amounts)
        changed = amount + shift if amount + shift != 0 else -amount  # never zero, never equal
        entries[index] = Entry(entries[index].account_id, changed, currency)
        self.reject(entries, UnbalancedEntriesError)

    @rule(data=st.data())
    def post_to_an_unknown_account(self, data) -> None:
        currency = data.draw(st.sampled_from(CURRENCIES))
        entries = self.balanced_group(data, currency)
        index = data.draw(st.integers(0, len(entries) - 1))
        unknown = max(self.accounts) + data.draw(st.integers(1, 1_000))
        entries[index] = Entry(unknown, entries[index].amount_minor, currency)
        self.reject(entries, UnknownAccountError)

    @rule(data=st.data())
    def post_in_the_wrong_currency(self, data) -> None:
        """Both entries are relabelled, so the other currency balances but the accounts disagree."""
        currency = data.draw(st.sampled_from(CURRENCIES))
        sender = data.draw(st.sampled_from(self.in_currency(currency)))
        receiver = data.draw(st.sampled_from(self.in_currency(currency)))
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        wrong = other_currency(currency)
        entries = [Entry(sender.id, -amount, wrong), Entry(receiver.id, amount, wrong)]
        self.reject(entries, CurrencyMismatchError)

    @rule(data=st.data())
    def post_balanced_overall_but_not_per_currency(self, data) -> None:
        """+x in one currency and -x in the other sum to zero in total, but not within either."""
        first = data.draw(st.sampled_from(CURRENCIES))
        sender = data.draw(st.sampled_from(self.in_currency(first)))
        receiver = data.draw(st.sampled_from(self.in_currency(other_currency(first))))
        amount = data.draw(st.integers(1, MAX_AMOUNT))
        entries = [
            Entry(sender.id, -amount, first),
            Entry(receiver.id, amount, other_currency(first)),
        ]
        self.reject(entries, UnbalancedEntriesError)

    @rule()
    def post_no_entries(self) -> None:
        self.reject([], LedgerInvariantError)

    # --- invariants, checked after every step ------------------------------------------------

    @invariant()
    def the_ledger_is_consistent(self) -> None:
        def scalar(sql: str) -> int:
            row = self.owner.execute(sql).fetchone()
            assert row is not None
            return row[0]

        assert not scalar(
            "SELECT count(*) FROM (SELECT 1 FROM entries GROUP BY transaction_id, currency"
            " HAVING sum(amount_minor) <> 0) t"
        ), "a transaction does not sum to zero in some currency"

        assert not scalar(
            "SELECT count(*) FROM (SELECT 1 FROM entries GROUP BY currency"
            " HAVING sum(amount_minor) <> 0) t"
        ), "a currency does not sum to zero across all accounts"

        assert not scalar(
            "SELECT count(*) FROM balances b"
            " LEFT JOIN (SELECT account_id, sum(amount_minor) AS total FROM entries"
            "            GROUP BY account_id) e ON e.account_id = b.account_id"
            " WHERE b.balance_minor <> COALESCE(e.total, 0)"
        ), "a cached balance differs from the sum of its entries"

        assert not scalar(
            "SELECT count(*) FROM balances WHERE NOT allow_negative AND balance_minor < 0"
        ), "a protected account is negative"

        assert not scalar(
            "SELECT count(*) FROM entries e JOIN accounts a ON a.id = e.account_id"
            " WHERE e.currency <> a.currency"
        ), "an entry's currency differs from its account's"

        assert self.committed_state()[2] == self.model, "the database and the model disagree"


LedgerMachine.TestCase.settings = settings(
    max_examples=50,
    stateful_step_count=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

TestLedgerProperties = LedgerMachine.TestCase
