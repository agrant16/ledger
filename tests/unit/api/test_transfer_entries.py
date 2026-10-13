"""entries_for_transfer (ledger/api/transfers.py; docs/design.md, Transfer request).

    entries_for_transfer(request: TransferRequest) -> list[Entry]

Builds the two entries of an ordinary transfer, separately from posting them: the `from` account
gets an entry of -amount_minor and the `to` account gets +amount_minor, both in the request's
currency, `from` first. It is a pure function, so the entries it returns always sum to zero.
The tests for unbalanced postings change its output and hand that to post_transaction.
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
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.api.transfers import entries_for_transfer

    return entries_for_transfer


def request(**overrides):
    from ledger.api.transfers import TransferRequest

    values = {"from_account_id": 12, "to_account_id": 34, "amount_minor": 2500, "currency": "USD"}
    return TransferRequest.model_validate({**values, **overrides})


def test_the_sender_is_debited_and_the_receiver_credited(build) -> None:
    assert build(request()) == [Entry(12, -2500, "USD"), Entry(34, 2500, "USD")]


def test_the_currency_and_the_amount_come_from_the_request(build) -> None:
    entries = build(request(amount_minor=1, currency="EUR"))
    assert entries == [Entry(12, -1, "EUR"), Entry(34, 1, "EUR")]


def test_the_largest_amount_is_not_changed(build) -> None:
    entries = build(request(amount_minor=MAX_AMOUNT))
    assert [e.amount_minor for e in entries] == [-MAX_AMOUNT, MAX_AMOUNT]


def test_building_twice_gives_equal_independent_lists(build) -> None:
    transfer = request()
    first, second = build(transfer), build(transfer)
    assert first == second
    assert first is not second


def test_unbalancing_the_output_is_a_matter_of_replacing_one_entry(build) -> None:
    """How the posting tests make an unbalanced set: Entry is frozen, so replace the last one."""
    entries = build(request())
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
    from ledger.api.transfers import entries_for_transfer

    sender, receiver = ids
    entries = entries_for_transfer(
        request(
            from_account_id=sender, to_account_id=receiver, amount_minor=amount, currency=currency
        )
    )

    assert len(entries) == 2
    assert sum(e.amount_minor for e in entries) == 0
    assert entries[0] == Entry(sender, -amount, currency)
    assert entries[1] == Entry(receiver, amount, currency)
    assert all(e.amount_minor != 0 for e in entries)
