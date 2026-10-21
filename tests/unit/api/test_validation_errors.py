"""Turning Pydantic validation errors into the `errors` list of an RFC 9457 problem body.

Contract these tests expect from ledger/api/rest/error_handlers.py:

    validation_errors(errors: list[dict]) -> list[dict]

`errors` is what `RequestValidationError.errors()` returns: dicts with at least `loc`, `msg` and
`type`. Each becomes one item, in order:

- a body field: {"detail": msg, "pointer": <RFC 6901 JSON Pointer>}, e.g. "/amount_minor". A
  problem with the whole body has the empty pointer "".
- a path, query, header or cookie parameter: {"detail": msg, "parameter": <its name>}.
- a location of any other kind, or none: {"detail": msg} alone. Claiming a pointer or a
  parameter for a place we cannot classify would mislead, and the function must never raise,
  because an exception inside the validation handler would turn a client's mistake into a 500.

Nothing else is passed on. In particular the client's `input` value is not echoed back.
"""

import pytest

from ledger.api.rest.error_handlers import validation_errors


def error(
    loc: tuple, msg: str = "Input should be a valid integer", type_: str = "int_type"
) -> dict:
    return {"loc": loc, "msg": msg, "type": type_, "input": "secret-input", "ctx": {"x": 1}}


@pytest.mark.parametrize(
    ("loc", "pointer"),
    [
        (("body", "amount_minor"), "/amount_minor"),
        (("body", "entries", 0, "amount_minor"), "/entries/0/amount_minor"),
        (("body", "a/b"), "/a~1b"),
        (("body", "a~b"), "/a~0b"),
        (("body",), ""),
    ],
)
def test_a_body_location_becomes_a_json_pointer(loc, pointer) -> None:
    assert validation_errors([error(loc)]) == [
        {"detail": "Input should be a valid integer", "pointer": pointer}
    ]


def test_malformed_json_points_at_the_whole_body() -> None:
    """Pydantic reports the character offset as the location (for example ("body", 20)), which is
    not a field, so a pointer of "/20" would mislead."""
    result = validation_errors([error(("body", 20), "JSON decode error", "json_invalid")])
    assert result == [{"detail": "JSON decode error", "pointer": ""}]


@pytest.mark.parametrize(
    ("loc", "name"),
    [
        (("path", "account_id"), "account_id"),
        (("query", "limit"), "limit"),
        (("header", "Idempotency-Key"), "Idempotency-Key"),
        (("cookie", "session"), "session"),
    ],
)
def test_a_parameter_location_is_named_not_pointed_at(loc, name) -> None:
    assert validation_errors([error(loc)]) == [
        {"detail": "Input should be a valid integer", "parameter": name}
    ]


def test_the_order_is_kept_and_nothing_else_is_passed_on() -> None:
    result = validation_errors(
        [
            error(("header", "Idempotency-Key"), "Field required", "missing"),
            error(("body", "currency"), "String should match pattern", "string_pattern_mismatch"),
        ]
    )
    assert [set(item) for item in result] == [{"detail", "parameter"}, {"detail", "pointer"}]
    assert "secret-input" not in str(result)


def test_no_errors_gives_an_empty_list() -> None:
    assert validation_errors([]) == []


# --- locations we do not expect: no locator, and never an exception -----------------------------


@pytest.mark.parametrize("loc", [("form", "username"), ("something-new", "x"), ("what",)])
def test_an_unknown_kind_of_location_gets_no_locator(loc) -> None:
    assert validation_errors([error(loc, "Odd")]) == [{"detail": "Odd"}]


def test_an_empty_location_gets_no_locator() -> None:
    assert validation_errors([error((), "Odd")]) == [{"detail": "Odd"}]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, {"detail": ""}),
        ({"msg": "Only a message"}, {"detail": "Only a message"}),
        ({"loc": ("body", "x")}, {"detail": "", "pointer": "/x"}),
        ({"type": "missing", "msg": "No location"}, {"detail": "No location"}),
    ],
)
def test_an_error_with_missing_keys_does_not_raise(raw, expected) -> None:
    assert validation_errors([raw]) == [expected]
