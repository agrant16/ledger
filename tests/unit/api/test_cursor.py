"""The entries cursor (ledger/api/rest/cursor.py; docs/design.md, GET /accounts/{id}/entries and Id
bounds; issue #82).

Contract these tests expect from ledger/api/rest/cursor.py:

    encode_cursor(entry_id: int) -> str
    decode_cursor(text: str) -> int

The cursor is the base64url of the last returned entry id, opaque to clients and unsigned. The
payload is the id written as plain decimal digits, encoded as base64url without `=` padding.

Decoding is strict: it accepts only text that `encode_cursor` could have produced, so each id has
exactly one valid cursor. Anything else raises InvalidRequestError (a 400, raised by the API layer
only), including text that a lenient decoder would still read as a valid id: padding, whitespace,
leading zeros, a sign, a different base64 spelling of the same bytes, non-ASCII digits. A decoded id
must be an integer from 1 to 9,223,372,036,854,775,807 (the id bounds). The error never repeats
the client's text.

No database is used.
"""

import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger.errors import InvalidRequestError, LedgerError

BIGINT_MAX = 9_223_372_036_854_775_807


@pytest.fixture
def cursor():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.api.rest import cursor

    return cursor


def b64(text: str) -> str:
    """The base64url of the text without padding, whatever the text means."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- encoding ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry_id", "expected"),
    [
        (1, "MQ"),
        (12, "MTI"),
        (BIGINT_MAX, "OTIyMzM3MjAzNjg1NDc3NTgwNw"),
    ],
)
def test_the_cursor_format_is_pinned(cursor, entry_id: int, expected: str) -> None:
    """Clients hold cursors between requests, so the format cannot change by accident."""
    assert cursor.encode_cursor(entry_id) == expected


@given(entry_id=st.integers(min_value=1, max_value=BIGINT_MAX))
def test_a_cursor_is_urlsafe_text_without_padding(entry_id: int) -> None:
    from ledger.api.rest import cursor

    text = cursor.encode_cursor(entry_id)

    assert isinstance(text, str)
    assert text != ""
    assert "=" not in text
    assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


# --- the round trip ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry_id", [1, 2, 9, 10, 99, 100, 255, 256, 65_536, BIGINT_MAX])
def test_decoding_an_encoded_id_gives_the_id_back(cursor, entry_id: int) -> None:
    assert cursor.decode_cursor(cursor.encode_cursor(entry_id)) == entry_id


@given(entry_id=st.integers(min_value=1, max_value=BIGINT_MAX))
def test_every_id_in_the_bounds_round_trips(entry_id: int) -> None:
    from ledger.api.rest import cursor

    assert cursor.decode_cursor(cursor.encode_cursor(entry_id)) == entry_id


@given(entry_id=st.integers(min_value=1, max_value=BIGINT_MAX))
def test_decode_then_encode_gives_the_same_text(entry_id: int) -> None:
    """There is exactly one cursor per id."""
    from ledger.api.rest import cursor

    text = cursor.encode_cursor(entry_id)

    assert cursor.encode_cursor(cursor.decode_cursor(text)) == text


# --- text that is not a cursor ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "!!!!",
        "M",
        "-_-_",
        "MQ MQ",
        "MQ/",
        "MQ+",
        "é",
        "٣",
        "MQé",
        "x" * 10_000,
    ],
    ids=repr,
)
def test_text_that_is_not_base64url_is_rejected(cursor, text: str) -> None:
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(text)


@pytest.mark.parametrize(
    "payload",
    ["abc", "id", "1e3", "1.0", "1,000", "1_000", "0x10", "٣", "١٢", "1\n1", ""],
    ids=repr,
)
def test_valid_base64url_that_is_not_plain_digits_is_rejected(cursor, payload: str) -> None:
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(b64(payload))


@pytest.mark.parametrize("payload", ["01", "007", "00", "+1", "-1", " 1", "1 ", "\t1", "1\n"])
def test_digits_with_a_sign_padding_or_leading_zero_are_rejected(cursor, payload: str) -> None:
    """These would all parse as an int, but they are not the form encode_cursor writes."""
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(b64(payload))


@pytest.mark.parametrize("text", ["MQ=", "MQ==", "MTI=", "MQ ", " MQ", "MQ\n", "\nMQ"])
def test_padding_and_whitespace_around_a_cursor_are_rejected(cursor, text: str) -> None:
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(text)


def test_another_base64_spelling_of_the_same_bytes_is_rejected(cursor) -> None:
    """'MR' decodes to the same byte as 'MQ' in a lenient decoder (the unused trailing bits are
    ignored), but encode_cursor(1) is only 'MQ'."""
    assert cursor.decode_cursor("MQ") == 1

    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor("MR")


# --- the id bounds ----------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["0", "9223372036854775808", "9" * 19, "1" + "0" * 40])
def test_an_id_outside_the_bounds_is_rejected(cursor, payload: str) -> None:
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(b64(payload))


@pytest.mark.parametrize("digits", [4_300, 4_301, 5_000, 100_000])
def test_a_cursor_of_thousands_of_digits_is_rejected_and_not_a_value_error(
    cursor, digits: int
) -> None:
    """Python refuses int() on a string of more than 4,300 digits with a ValueError, which would
    reach the API as a 500. The length has to be checked before the number is parsed."""
    with pytest.raises(InvalidRequestError):
        cursor.decode_cursor(b64("1" * digits))


def test_the_largest_id_is_accepted(cursor) -> None:
    assert cursor.decode_cursor(b64(str(BIGINT_MAX))) == BIGINT_MAX


def test_the_smallest_id_is_accepted(cursor) -> None:
    assert cursor.decode_cursor(b64("1")) == 1


# --- the error --------------------------------------------------------------------------------


def test_the_error_is_an_invalid_request_error(cursor) -> None:
    with pytest.raises(InvalidRequestError) as failure:
        cursor.decode_cursor("!!!!")

    assert isinstance(failure.value, LedgerError)


@pytest.mark.parametrize("text", ["SECRETSECRET!", b64("SECRETSECRET"), b64("9" * 25)])
def test_the_error_does_not_repeat_the_clients_text(cursor, text: str) -> None:
    """Design: the client's input is never echoed back."""
    with pytest.raises(InvalidRequestError) as failure:
        cursor.decode_cursor(text)

    assert text not in str(failure.value)
    assert "SECRETSECRET" not in str(failure.value)


# --- anything at all --------------------------------------------------------------------------


@given(text=st.text())
def test_decoding_any_text_returns_a_valid_id_or_raises_invalid_request(text: str) -> None:
    """Nothing else can come out of the decoder: not a ValueError, not an id out of range."""
    from ledger.api.rest import cursor

    try:
        entry_id = cursor.decode_cursor(text)
    except InvalidRequestError:
        return

    assert 1 <= entry_id <= BIGINT_MAX
    assert cursor.encode_cursor(entry_id) == text
