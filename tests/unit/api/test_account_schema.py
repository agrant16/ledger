"""CreateAccountRequest (ledger/api/rest/schemas/account_schema.py; docs/design.md, Account
request and response).

    CreateAccountRequest(account_name, currency)

A pydantic model that uses the AccountName and Currency types, so their rules are checked in
test_types.py and only the shape of the model is checked here: both fields are required, unknown
fields are rejected (which is how `allow_negative` is kept out of the API), and every problem is
reported together.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

VALID = {"account_name": "customer:alice", "currency": "USD"}


@pytest.fixture
def model():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.api.rest.schemas.account_schema import CreateAccountRequest

    return CreateAccountRequest


def error_types(failure: ValidationError) -> set[tuple[tuple, str]]:
    return {(error["loc"], error["type"]) for error in failure.value.errors()}


def test_a_valid_request(model) -> None:
    request = model.model_validate(VALID)
    assert (request.account_name, request.currency) == ("customer:alice", "USD")


@pytest.mark.parametrize("missing", ["account_name", "currency"])
def test_each_field_is_required(model, missing) -> None:
    body = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    assert ((missing,), "missing") in error_types(failure)


@pytest.mark.parametrize(
    "extra", [{"allow_negative": True}, {"allow_negative": False}, {"nmae": "x"}]
)
def test_unknown_fields_are_rejected(model, extra) -> None:
    """allow_negative is rejected like any typo: the API cannot create an account that may go
    negative."""
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, **extra})
    (field,) = extra
    assert ((field,), "extra_forbidden") in error_types(failure)


@pytest.mark.parametrize("body", [None, [], "alice", 5])
def test_a_body_that_is_not_an_object_is_rejected(model, body) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_name", " padded"),
        ("account_name", ""),
        ("account_name", 5),
        ("currency", "usd"),
        ("currency", None),
    ],
)
def test_the_fields_use_the_shared_rules(model, field, value) -> None:
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, field: value})
    assert any(loc == (field,) for loc, _ in error_types(failure))


def test_every_problem_is_reported_together(model) -> None:
    with pytest.raises(ValidationError) as failure:
        model.model_validate({"account_name": " bad", "currency": "usd", "allow_negative": True})
    locations = {loc for loc, _ in error_types(failure)}
    assert locations == {("account_name",), ("currency",), ("allow_negative",)}


def test_through_a_route_a_bad_request_is_a_400_with_pointers(model) -> None:
    from ledger.api.rest.error_handlers import install_error_handling

    app = FastAPI()
    install_error_handling(app)

    @app.post("/accounts")
    def create(body: model) -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)

    good = client.post("/accounts", json=VALID)
    assert good.status_code == 200

    bad = client.post(
        "/accounts", json={"account_name": "x" * 101, "currency": "usd", "allow_negative": True}
    )
    assert bad.status_code == 400
    pointers = {item["pointer"] for item in bad.json()["errors"]}
    assert pointers == {"/account_name", "/currency", "/allow_negative"}


# --- the response schemas convert from the domain models --------------------------------------
#
# The conversion logic lives in the schemas, not in the routes: a route hands over the model the
# service returned, and the schema decides what the wire shape is.


def test_an_account_response_is_built_from_the_account_model() -> None:
    from ledger.api.rest.schemas.account_schema import AccountResponse
    from ledger.models.account_model import Account

    response = AccountResponse.from_model(Account(7, "customer:alice", "EUR", False))

    assert response == AccountResponse(
        id=7, account_name="customer:alice", currency="EUR", allow_negative=False
    )
    assert response.model_dump() == {
        "id": 7,
        "account_name": "customer:alice",
        "currency": "EUR",
        "allow_negative": False,
    }


def test_the_account_response_carries_allow_negative_as_the_model_has_it() -> None:
    from ledger.api.rest.schemas.account_schema import AccountResponse
    from ledger.models.account_model import Account

    assert AccountResponse.from_model(Account(1, "funding:usd", "USD", True)).allow_negative is True


def test_a_balance_response_is_built_from_the_balance_model() -> None:
    from ledger.api.rest.schemas.account_schema import BalanceResponse
    from ledger.models.account_model import Balance

    response = BalanceResponse.from_model(Balance(7, "USD", -300))

    assert response.model_dump() == {"account_id": 7, "currency": "USD", "balance_minor": -300}


def test_the_response_schemas_have_exactly_the_documented_fields() -> None:
    from ledger.api.rest.schemas.account_schema import AccountResponse, BalanceResponse

    assert set(AccountResponse.model_fields) == {"id", "account_name", "currency", "allow_negative"}
    assert set(BalanceResponse.model_fields) == {"account_id", "currency", "balance_minor"}
