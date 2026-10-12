"""TransferRequest (ledger/api/transfers.py; docs/design.md, Transfer request).

    TransferRequest(from_account_id, to_account_id, amount_minor, currency)

A pydantic model built from the AccountId, AmountMinor and Currency types (their rules are checked
in test_types.py). What is checked here is the shape: every field is required, a value of the
wrong kind is never converted, unknown fields are rejected, the two accounts must differ, and
every problem is reported together.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

VALID = {"from_account_id": 12, "to_account_id": 34, "amount_minor": 2500, "currency": "USD"}
INTEGER_FIELDS = ["from_account_id", "to_account_id", "amount_minor"]


@pytest.fixture
def model():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.api.transfers import TransferRequest

    return TransferRequest


def error_types(failure) -> set[tuple[tuple, str]]:
    return {(error["loc"], error["type"]) for error in failure.value.errors()}


def value_errors(failure, field: str) -> set[str]:
    """The error types reported for a field's value. 'missing' and 'extra_forbidden' are about the
    field's name, not its value, so a test about a bad value must not be satisfied by them."""
    return {t for loc, t in error_types(failure) if loc == (field,)} - {
        "missing",
        "extra_forbidden",
    }


def test_the_model_has_exactly_the_documented_fields(model) -> None:
    """The API's field names, from the design doc's example body."""
    assert set(model.model_fields) == set(VALID)


def test_a_valid_request(model) -> None:
    request = model.model_validate(VALID)
    assert (
        request.from_account_id,
        request.to_account_id,
        request.amount_minor,
        request.currency,
    ) == (12, 34, 2500, "USD")


@pytest.mark.parametrize("missing", list(VALID))
def test_each_field_is_required(model, missing) -> None:
    body = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    assert ((missing,), "missing") in error_types(failure)


@pytest.mark.parametrize("field", INTEGER_FIELDS)
@pytest.mark.parametrize("value", ["5", 5.0, 1.5, True, False, None, [5]])
def test_integers_are_never_converted_from_other_kinds(model, field, value) -> None:
    """Strings, floats (even whole ones), booleans and null are rejected, not coerced."""
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, field: value})
    assert value_errors(failure, field)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_account_id", 0),
        ("from_account_id", -3),
        ("from_account_id", 9_223_372_036_854_775_808),
        ("to_account_id", 0),
        ("to_account_id", 9_223_372_036_854_775_808),
        ("amount_minor", 0),
        ("amount_minor", -1),
        ("amount_minor", 99_900_000_001),
        ("currency", "usd"),
        ("currency", "USDD"),
        ("currency", 5),
    ],
)
def test_the_fields_use_the_shared_rules(model, field, value) -> None:
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, field: value})
    assert value_errors(failure, field)


@pytest.mark.parametrize(
    "values",
    [
        {"from_account_id": 1, "to_account_id": 9_223_372_036_854_775_807},
        {"amount_minor": 1},
        {"amount_minor": 99_900_000_000},
    ],
)
def test_the_boundaries_are_accepted(model, values) -> None:
    model.model_validate({**VALID, **values})


def test_a_typo_in_a_field_name_is_an_error_and_not_ignored(model) -> None:
    body = {k: v for k, v in VALID.items() if k != "amount_minor"} | {"ammount_minor": 2500}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    kinds = error_types(failure)
    assert (("ammount_minor",), "extra_forbidden") in kinds
    assert (("amount_minor",), "missing") in kinds


def test_an_extra_field_is_rejected(model) -> None:
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, "idempotency_key": "k"})
    assert (("idempotency_key",), "extra_forbidden") in error_types(failure)


@pytest.mark.parametrize("body", [None, [], "transfer", 5])
def test_a_body_that_is_not_an_object_is_rejected(model, body) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_moving_money_to_the_same_account_is_rejected(model) -> None:
    """The error is about the whole body, so its location is empty, and it is not about any one
    field (which would mean a field name or value was wrong instead)."""
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, "to_account_id": VALID["from_account_id"]})
    assert error_types(failure) == {((), "value_error")}


def test_every_problem_is_reported_together(model) -> None:
    body = {**VALID, "from_account_id": 0, "amount_minor": 0, "currency": "usd"}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    locations = {loc for loc, _ in error_types(failure)}
    assert locations == {("from_account_id",), ("amount_minor",), ("currency",)}


def test_through_a_route_the_json_kinds_are_strict(model) -> None:
    """A whole-number float, a string, a boolean and null all arrive as JSON, so check them as JSON
    text and see the 400 with a pointer at the field."""
    from ledger.api.errors import install_error_handling

    app = FastAPI()
    install_error_handling(app)

    @app.post("/transfers")
    def create(body: model) -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Content-Type": "application/json"}

    good = client.post("/transfers", json=VALID)
    assert good.status_code == 200

    for raw in ["2500.0", '"2500"', "true", "null", "1e3"]:
        text = (
            '{"from_account_id": 12, "to_account_id": 34, '
            + f'"amount_minor": {raw}, "currency": "USD"}}'
        )
        response = client.post("/transfers", content=text, headers=headers)
        assert response.status_code == 400, raw
        assert response.json()["errors"][0]["pointer"] == "/amount_minor", raw

    same = client.post("/transfers", json={**VALID, "to_account_id": 12})
    assert same.status_code == 400
    assert same.json()["errors"][0]["pointer"] == ""
