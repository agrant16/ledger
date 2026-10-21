"""request_hash (ledger/api/rest/idempotency.py; docs/design.md, Request hash).

    request_hash(method, path, body) -> str

The hash that goes in `transactions.request_hash`: 64 lowercase hex digits, the SHA-256 of the
canonical JSON of the list [METHOD, path, body]. It is computed from the parsed request, so a
harmless difference between retries (key order, spacing, a lowercase method, a default written out)
never looks like a different request, and a real difference always does. `body` is the validated
request model, or None for a request with no body (a reversal).

Stored hashes are compared with new ones on every replay, so the exact form is a contract:
changing it would make every old key look like a conflict. One test pins it.
"""

import hashlib
import json
import re

import pytest
from pydantic import BaseModel

from ledger.api.rest.idempotency import request_hash
from ledger.api.rest.schemas.account_schema import CreateAccountRequest
from ledger.api.rest.schemas.transfer_schema import TransferRequest

TRANSFER = {"from_account_id": 12, "to_account_id": 34, "amount_minor": 2500, "currency": "USD"}
TRANSFER_JSON = json.dumps(TRANSFER)


@pytest.fixture
def transfer_model():
    return TransferRequest


@pytest.fixture
def transfer(transfer_model):
    return transfer_model.model_validate(TRANSFER)


@pytest.fixture
def account_model():
    return CreateAccountRequest


def test_it_is_64_lowercase_hex_digits(transfer) -> None:
    result = request_hash("POST", "/transfers", transfer)

    assert re.fullmatch(r"[0-9a-f]{64}", result)


def test_the_same_request_always_gives_the_same_hash(transfer_model) -> None:
    first = request_hash("POST", "/transfers", transfer_model.model_validate(TRANSFER))
    second = request_hash("POST", "/transfers", transfer_model.model_validate(TRANSFER))

    assert first == second


def test_the_form_is_pinned_to_the_sha256_of_the_canonical_json(transfer) -> None:
    """Sorted keys, no spaces, and the method, path and body as one list."""
    canonical = (
        '["POST","/transfers",{"amount_minor":2500,"currency":"USD",'
        '"from_account_id":12,"to_account_id":34}]'
    )

    result = request_hash("POST", "/transfers", transfer)

    assert result == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_request_with_no_body_hashes_a_json_null() -> None:
    canonical = '["POST","/transfers/5/reverse",null]'

    result = request_hash("POST", "/transfers/5/reverse", None)

    assert result == hashlib.sha256(canonical.encode()).hexdigest()


# --- harmless differences hash the same ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"currency":"USD","amount_minor":2500,"to_account_id":34,"from_account_id":12}',
        '{\n  "to_account_id": 34,\n\t"from_account_id": 12,\n  "currency": "USD",'
        ' "amount_minor": 2500\n}',
        '  {"from_account_id":12 , "to_account_id" : 34,"amount_minor":2500,"currency":"USD"}  ',
    ],
    ids=["reordered keys", "reordered keys and whitespace", "padding around the body"],
)
def test_key_order_and_whitespace_in_the_body_do_not_matter(transfer_model, raw: str) -> None:
    """The body is parsed first, so only its meaning is hashed."""
    expected = request_hash("POST", "/transfers", transfer_model.model_validate_json(TRANSFER_JSON))

    assert request_hash("POST", "/transfers", transfer_model.model_validate_json(raw)) == expected


def test_the_method_is_hashed_in_uppercase(transfer) -> None:
    assert request_hash("post", "/transfers", transfer) == request_hash(
        "POST", "/transfers", transfer
    )


def test_a_default_written_out_hashes_the_same_as_leaving_it_out() -> None:
    """No request model has an optional field yet, so a throwaway one stands in for the rule:
    the hash covers the validated model, defaults included."""

    class WithDefault(BaseModel):
        amount_minor: int
        memo: str = "none"

    omitted = WithDefault.model_validate_json('{"amount_minor": 5}')
    written = WithDefault.model_validate_json('{"amount_minor": 5, "memo": "none"}')
    different = WithDefault.model_validate_json('{"amount_minor": 5, "memo": "rent"}')

    assert request_hash("POST", "/x", omitted) == request_hash("POST", "/x", written)
    assert request_hash("POST", "/x", omitted) != request_hash("POST", "/x", different)


# --- real differences hash differently ----------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("from_account_id", 13),
        ("to_account_id", 35),
        ("amount_minor", 2501),
        ("currency", "EUR"),
    ],
)
def test_changing_any_body_field_changes_the_hash(
    transfer_model, transfer, field: str, value
) -> None:
    changed = transfer_model.model_validate({**TRANSFER, field: value})

    assert request_hash("POST", "/transfers", changed) != request_hash(
        "POST", "/transfers", transfer
    )


def test_swapping_the_two_accounts_changes_the_hash(transfer_model, transfer) -> None:
    """The same values under different keys: a transfer the other way round is another request."""
    swapped = transfer_model.model_validate(
        {**TRANSFER, "from_account_id": 34, "to_account_id": 12}
    )

    assert request_hash("POST", "/transfers", swapped) != request_hash(
        "POST", "/transfers", transfer
    )


def test_a_different_method_changes_the_hash(transfer) -> None:
    assert request_hash("POST", "/transfers", transfer) != request_hash(
        "PUT", "/transfers", transfer
    )


def test_a_different_path_changes_the_hash(transfer) -> None:
    assert request_hash("POST", "/transfers", transfer) != request_hash(
        "POST", "/accounts", transfer
    )


def test_a_reversal_of_a_different_transfer_changes_the_hash() -> None:
    """The target is in the path, so reversing transfer 5 and transfer 6 are different requests."""
    assert request_hash("POST", "/transfers/5/reverse", None) != request_hash(
        "POST", "/transfers/6/reverse", None
    )


def test_a_body_is_different_from_no_body(transfer) -> None:
    assert request_hash("POST", "/transfers", transfer) != request_hash("POST", "/transfers", None)


# --- text ---------------------------------------------------------------------------------------


def test_non_ascii_text_hashes_the_same_every_time(account_model) -> None:
    body = {
        "account_name": "customer:\N{LATIN SMALL LETTER E WITH ACUTE}\U0001f600",
        "currency": "USD",
    }

    first = request_hash("POST", "/accounts", account_model.model_validate(body))
    second = request_hash("POST", "/accounts", account_model.model_validate(body))

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_text_is_not_normalized(account_model) -> None:
    """A precomposed e-acute and an e followed by a combining accent are different strings, and
    the database stores them as different names, so they are different requests."""
    precomposed = {"account_name": "caf\N{LATIN SMALL LETTER E WITH ACUTE}", "currency": "USD"}
    decomposed = {"account_name": "cafe\N{COMBINING ACUTE ACCENT}", "currency": "USD"}

    assert request_hash(
        "POST", "/accounts", account_model.model_validate(precomposed)
    ) != request_hash("POST", "/accounts", account_model.model_validate(decomposed))
