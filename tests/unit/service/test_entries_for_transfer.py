"""entries_for_transfer (ledger/service/transfer_service.py; docs/design.md, Transfer request).

    entries_for_transfer(from_account_id, to_account_id, amount_minor, currency) -> list[Entry]

Builds the two entries of an ordinary transfer, separately from posting them: the `from` account
gets an entry of -amount_minor and the `to` account gets +amount_minor, both in the given currency,
`from` first. It is a pure function of plain values, so the entries it returns always sum to zero.
The tests for unbalanced postings change its output and hand that to post_transaction.

It lives in the service layer, not the API layer: building the entries is a ledger rule, and the
service flow that claims the key, builds the entries and posts them uses it. The API layer hands
over the validated values and never builds an Entry itself. It does not validate: an amount of zero
or a transfer to the same account is rejected by the API's request schema before it gets here.
"""

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger.models.posting_model import Entry

BIGINT_MAX = 9_223_372_036_854_775_807
MAX_AMOUNT = 99_900_000_000


@pytest.fixture
def build():
    """Imported lazily so these tests fail one by one while the function does not exist yet."""
    from ledger.service.transfer_service import entries_for_transfer

    return entries_for_transfer


def test_the_sender_is_debited_and_the_receiver_credited(build) -> None:
    assert build(12, 34, 2500, "USD") == [Entry(12, -2500, "USD"), Entry(34, 2500, "USD")]


def test_the_arguments_can_be_given_by_name(build) -> None:
    entries = build(from_account_id=12, to_account_id=34, amount_minor=2500, currency="USD")

    assert entries == [Entry(12, -2500, "USD"), Entry(34, 2500, "USD")]


def test_the_currency_and_the_amount_come_from_the_arguments(build) -> None:
    assert build(12, 34, 1, "EUR") == [Entry(12, -1, "EUR"), Entry(34, 1, "EUR")]


def test_the_largest_amount_is_not_changed(build) -> None:
    entries = build(12, 34, MAX_AMOUNT, "USD")

    assert [e.amount_minor for e in entries] == [-MAX_AMOUNT, MAX_AMOUNT]


def test_building_twice_gives_equal_independent_lists(build) -> None:
    first, second = build(12, 34, 2500, "USD"), build(12, 34, 2500, "USD")

    assert first == second
    assert first is not second


def test_unbalancing_the_output_is_a_matter_of_replacing_one_entry(build) -> None:
    """How the posting tests make an unbalanced set: Entry is frozen, so replace the last one."""
    entries = build(12, 34, 2500, "USD")
    entries[-1] = dataclasses.replace(entries[-1], amount_minor=entries[-1].amount_minor + 1)

    assert sum(e.amount_minor for e in entries) == 1


DISTINCT_IDS = st.tuples(st.integers(1, BIGINT_MAX), st.integers(1, BIGINT_MAX)).filter(
    lambda pair: pair[0] != pair[1]
)


@given(
    ids=DISTINCT_IDS,
    amount=st.integers(1, MAX_AMOUNT),
    currency=st.from_regex(r"[A-Z]{3}", fullmatch=True),
)
def test_the_entries_always_balance(ids, amount, currency) -> None:
    from ledger.service.transfer_service import entries_for_transfer

    sender, receiver = ids
    entries = entries_for_transfer(sender, receiver, amount, currency)

    assert len(entries) == 2
    assert sum(e.amount_minor for e in entries) == 0
    assert entries[0] == Entry(sender, -amount, currency)
    assert entries[1] == Entry(receiver, amount, currency)
    assert all(e.amount_minor != 0 for e in entries)
