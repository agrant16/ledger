"""TransferRequest (ledger/api/rest/schemas/transfer_schema.py; docs/design.md, Transfer request).

    TransferRequest(from_account_id, to_account_id, amount_minor, currency)

A pydantic model built from the AccountId, AmountMinor and Currency types (their rules are checked
in test_types.py). What is checked here is the shape: every field is required, a value of the
wrong kind is never converted, unknown fields are rejected, the two accounts must differ, and
every problem is reported together.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ledger.api.rest.schemas.transfer_schema import TransferRequest as model

VALID = {"from_account_id": 12, "to_account_id": 34, "amount_minor": 2500, "currency": "USD"}
INTEGER_FIELDS = ["from_account_id", "to_account_id", "amount_minor"]


def error_types(failure) -> set[tuple[tuple, str]]:
    return {(error["loc"], error["type"]) for error in failure.value.errors()}


def value_errors(failure, field: str) -> set[str]:
    """The error types reported for a field's value. 'missing' and 'extra_forbidden' are about the
    field's name, not its value, so a test about a bad value must not be satisfied by them."""
    return {t for loc, t in error_types(failure) if loc == (field,)} - {
        "missing",
        "extra_forbidden",
    }


def test_the_model_has_exactly_the_documented_fields() -> None:
    """The API's field names, from the design doc's example body."""
    assert set(model.model_fields) == set(VALID)


def test_a_valid_request() -> None:
    request = model.model_validate(VALID)
    assert (
        request.from_account_id,
        request.to_account_id,
        request.amount_minor,
        request.currency,
    ) == (12, 34, 2500, "USD")


@pytest.mark.parametrize("missing", list(VALID))
def test_each_field_is_required(missing) -> None:
    body = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    assert ((missing,), "missing") in error_types(failure)


@pytest.mark.parametrize("field", INTEGER_FIELDS)
@pytest.mark.parametrize("value", ["5", 5.0, 1.5, True, False, None, [5]])
def test_integers_are_never_converted_from_other_kinds(field, value) -> None:
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
def test_the_fields_use_the_shared_rules(field, value) -> None:
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
def test_the_boundaries_are_accepted(values) -> None:
    model.model_validate({**VALID, **values})


def test_a_typo_in_a_field_name_is_an_error_and_not_ignored() -> None:
    body = {k: v for k, v in VALID.items() if k != "amount_minor"} | {"ammount_minor": 2500}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    kinds = error_types(failure)
    assert (("ammount_minor",), "extra_forbidden") in kinds
    assert (("amount_minor",), "missing") in kinds


def test_an_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, "idempotency_key": "k"})
    assert (("idempotency_key",), "extra_forbidden") in error_types(failure)


@pytest.mark.parametrize("body", [None, [], "transfer", 5])
def test_a_body_that_is_not_an_object_is_rejected(body) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_moving_money_to_the_same_account_is_rejected() -> None:
    """The error is about the whole body, so its location is empty, and it is not about any one
    field (which would mean a field name or value was wrong instead)."""
    with pytest.raises(ValidationError) as failure:
        model.model_validate({**VALID, "to_account_id": VALID["from_account_id"]})
    assert error_types(failure) == {((), "value_error")}


def test_every_problem_is_reported_together() -> None:
    body = {**VALID, "from_account_id": 0, "amount_minor": 0, "currency": "usd"}
    with pytest.raises(ValidationError) as failure:
        model.model_validate(body)
    locations = {loc for loc, _ in error_types(failure)}
    assert locations == {("from_account_id",), ("amount_minor",), ("currency",)}


def test_through_a_route_the_json_kinds_are_strict() -> None:
    """A whole-number float, a string, a boolean and null all arrive as JSON, so check them as JSON
    text and see the 400 with a pointer at the field."""
    from ledger.api.rest.error_handlers import install_error_handling

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


# --- TransferResponse -------------------------------------------------------------------------
#
#     TransferResponse.from_model(Transfer) -> id, reverses_transaction_id, created_at, entries
#
# `created_at` is ISO 8601 in UTC with a trailing Z, built here from the model's aware datetime.
# This is the body of GET /transfers/{id} and, later, of POST /transfers, byte for byte.

TOKYO = timezone(timedelta(hours=9))


def transfer(created_at: datetime, reverses: int | None = None):
    from ledger.models.transfer_model import Transfer, TransferEntry

    return Transfer(
        id=7,
        reverses_transaction_id=reverses,
        created_at=created_at,
        entries=(TransferEntry(1, 10, -500, "USD"), TransferEntry(2, 11, 500, "USD")),
    )


@pytest.fixture
def response_model():
    from ledger.api.rest.schemas.transfer_schema import TransferResponse

    return TransferResponse


def test_the_response_has_exactly_the_documented_fields(response_model) -> None:
    body = response_model.from_model(transfer(datetime(2026, 10, 13, 10, 11, 12, tzinfo=UTC)))

    assert body.model_dump(mode="json") == {
        "id": 7,
        "reverses_transaction_id": None,
        "created_at": "2026-10-13T10:11:12Z",
        "entries": [
            {"id": 1, "account_id": 10, "amount_minor": -500, "currency": "USD"},
            {"id": 2, "account_id": 11, "amount_minor": 500, "currency": "USD"},
        ],
    }


def test_the_response_carries_the_reversed_id(response_model) -> None:
    body = response_model.from_model(transfer(datetime(2026, 1, 1, tzinfo=UTC), reverses=3))

    assert body.model_dump(mode="json")["reverses_transaction_id"] == 3


def test_microseconds_are_kept_and_the_time_ends_in_z(response_model) -> None:
    created = datetime(2026, 10, 13, 10, 11, 12, 123456, tzinfo=UTC)

    body = response_model.from_model(transfer(created)).model_dump(mode="json")

    assert body["created_at"] == "2026-10-13T10:11:12.123456Z"


def test_a_time_in_another_zone_is_written_as_the_same_instant_in_utc(response_model) -> None:
    created = datetime(2026, 10, 13, 19, 11, 12, tzinfo=TOKYO)

    body = response_model.from_model(transfer(created)).model_dump(mode="json")

    assert body["created_at"] == "2026-10-13T10:11:12Z"
