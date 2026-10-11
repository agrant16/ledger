"""Turning Pydantic validation errors into the `errors` list of an RFC 9457 problem body.

Contract these tests expect from ledger/api/errors.py:

    validation_errors(errors: list[dict]) -> list[dict]

`errors` is what `RequestValidationError.errors()` returns: dicts with at least `loc`, `msg` and
`type`. Each becomes one item, in order:

- a body field: {"detail": msg, "pointer": <RFC 6901 JSON Pointer>}, e.g. "/amount_minor". A
  problem with the whole body has the empty pointer "".
- a path, query or header parameter: {"detail": msg, "parameter": <its name>}.

Nothing else is passed on. In particular the client's `input` value is not echoed back.
"""

import pytest


@pytest.fixture
def convert():
    from ledger.api.errors import validation_errors

    return validation_errors


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
def test_a_body_location_becomes_a_json_pointer(convert, loc, pointer) -> None:
    assert convert([error(loc)]) == [
        {"detail": "Input should be a valid integer", "pointer": pointer}
    ]


def test_malformed_json_points_at_the_whole_body(convert) -> None:
    """Pydantic reports the character offset as the location (for example ("body", 20)), which is
    not a field, so a pointer of "/20" would mislead."""
    result = convert([error(("body", 20), "JSON decode error", "json_invalid")])
    assert result == [{"detail": "JSON decode error", "pointer": ""}]


@pytest.mark.parametrize(
    ("loc", "name"),
    [
        (("path", "account_id"), "account_id"),
        (("query", "limit"), "limit"),
        (("header", "Idempotency-Key"), "Idempotency-Key"),
    ],
)
def test_a_parameter_location_is_named_not_pointed_at(convert, loc, name) -> None:
    assert convert([error(loc)]) == [
        {"detail": "Input should be a valid integer", "parameter": name}
    ]


def test_the_order_is_kept_and_nothing_else_is_passed_on(convert) -> None:
    result = convert(
        [
            error(("header", "Idempotency-Key"), "Field required", "missing"),
            error(("body", "currency"), "String should match pattern", "string_pattern_mismatch"),
        ]
    )
    assert [set(item) for item in result] == [{"detail", "parameter"}, {"detail", "pointer"}]
    assert "secret-input" not in str(result)


def test_no_errors_gives_an_empty_list(convert) -> None:
    assert convert([]) == []
