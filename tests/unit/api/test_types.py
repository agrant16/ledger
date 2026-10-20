"""The shared request types (ledger/api/rest/schemas/types.py; docs/design.md, Transfer request,
Account request and response, Id bounds). Their rules are checked here once, so the request models
only need to show they use them.

Contract these tests expect from ledger/api/rest/schemas/types.py. Each name is an `Annotated` type
that works with pydantic's TypeAdapter:

    AccountId        a JSON body id: a strict integer from 1 to 9,223,372,036,854,775,807
    AccountIdInPath  the same id as it arrives in a URL: a string of canonical decimal digits
                     (no sign, spaces, underscores, leading zeros or decimal point), converted to
                     an int and range-checked; usable as a FastAPI path parameter
    TransferIdInPath a transfer id in a URL, with the same rules as AccountIdInPath
    AmountMinor      a strict integer from 1 to 99,900,000,000
    Currency         a string of exactly three uppercase ASCII letters
    AccountName      1 to 100 characters (code points); no leading or trailing space, tab,
                     newline, vertical tab, form feed or carriage return; no NUL; no surrogates
    IdempotencyKey   1 to 255 characters; no NUL; no surrogates

Strictness: a value of the wrong kind is rejected and not converted, so 12.0, "12", True and None
are not ids or amounts, and 123 is not a currency.
"""

from typing import Annotated

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from ledger.api.rest.schemas import types

BIGINT_MAX = 9_223_372_036_854_775_807
MAX_AMOUNT = 99_900_000_000


def accepts(adapter: TypeAdapter, value: object) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


# --- account ids in a JSON body ---------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 12, BIGINT_MAX])
def test_an_account_id_in_a_body_accepts_integers_in_range(value) -> None:
    assert TypeAdapter(types.AccountId).validate_python(value) == value


@pytest.mark.parametrize(
    "value", [0, -1, BIGINT_MAX + 1, 12.0, 1.5, "12", True, False, None, [1], {"id": 1}]
)
def test_an_account_id_in_a_body_rejects_everything_else(value) -> None:
    assert not accepts(TypeAdapter(types.AccountId), value)


# --- account ids in a URL ---------------------------------------------------------------------


@pytest.fixture
def path_client() -> TestClient:
    from ledger.api.rest.error_handlers import install_error_handling

    app = FastAPI()
    install_error_handling(app)

    @app.get("/accounts/{account_id}")
    def read(account_id: types.AccountIdInPath) -> dict[str, int]:
        return {"id": account_id}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("raw", ["1", "12", "10", "100", "205", "9000", str(BIGINT_MAX)])
def test_an_account_id_in_a_path_accepts_canonical_digits(path_client, raw) -> None:
    """Includes ids with zeros inside and at the end (10, 100, 205, 9000): zero is a valid digit
    anywhere but the front."""
    response = path_client.get(f"/accounts/{raw}")
    assert response.status_code == 200
    assert response.json() == {"id": int(raw)}


@pytest.mark.parametrize(
    "raw",
    [
        "0",
        "00",
        "-1",
        "012",  # a leading zero
        "%2B12",  # +12
        "%2012",  # a leading space
        "12%20",  # a trailing space
        "1_000",
        "12.0",
        "1e3",
        "abc",
        str(BIGINT_MAX + 1),
        "%D9%A3",  # an Arabic-Indic digit three
    ],
)
def test_an_account_id_in_a_path_rejects_anything_but_canonical_digits(path_client, raw) -> None:
    response = path_client.get(f"/accounts/{raw}")
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "urn:ledger:error:invalid-request"
    assert body["errors"][0]["parameter"] == "account_id"


@pytest.fixture
def transfer_path_client() -> TestClient:
    from ledger.api.rest.error_handlers import install_error_handling

    app = FastAPI()
    install_error_handling(app)

    @app.get("/transfers/{transfer_id}")
    def read(transfer_id: types.TransferIdInPath) -> dict[str, int]:
        return {"id": transfer_id}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("raw", ["1", "10", "205", str(BIGINT_MAX)])
def test_a_transfer_id_in_a_path_accepts_canonical_digits(transfer_path_client, raw) -> None:
    response = transfer_path_client.get(f"/transfers/{raw}")

    assert response.status_code == 200
    assert response.json() == {"id": int(raw)}


@pytest.mark.parametrize(
    "raw", ["0", "-1", "012", "%2B12", "%2012", "1_000", "12.0", "abc", str(BIGINT_MAX + 1)]
)
def test_a_transfer_id_in_a_path_rejects_anything_but_canonical_digits(
    transfer_path_client, raw
) -> None:
    response = transfer_path_client.get(f"/transfers/{raw}")

    assert response.status_code == 400
    assert response.json()["errors"][0]["parameter"] == "transfer_id"


# --- amounts ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2500, MAX_AMOUNT])
def test_an_amount_accepts_integers_from_one_to_the_cap(value) -> None:
    assert TypeAdapter(types.AmountMinor).validate_python(value) == value


@pytest.mark.parametrize("value", [0, -1, MAX_AMOUNT + 1, BIGINT_MAX, 5.0, 0.5, "5", True, None])
def test_an_amount_rejects_everything_else(value) -> None:
    assert not accepts(TypeAdapter(types.AmountMinor), value)


# --- currencies -------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["USD", "EUR", "ABC"])
def test_a_currency_accepts_three_uppercase_letters(value) -> None:
    assert TypeAdapter(types.Currency).validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "usd",
        "Usd",
        "US",
        "USDD",
        "",
        "U1D",
        "US$",
        "USD\n",  # Postgres's $ matches only at the very end, and so must this
        " USD",
        "USD ",
        (  # fullwidth USD
            "\N{FULLWIDTH LATIN CAPITAL LETTER U}"
            "\N{FULLWIDTH LATIN CAPITAL LETTER S}"
            "\N{FULLWIDTH LATIN CAPITAL LETTER D}"
        ),
        "ÜSD",
        123,
        None,
        ["USD"],
        b"USD",  # bytes are not converted to text
    ],
)
def test_a_currency_rejects_everything_else(value) -> None:
    assert not accepts(TypeAdapter(types.Currency), value)


# --- account names ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "customer:alice",
        "a b",  # inner whitespace is fine
        "tab\tinside",
        "x" * 100,
        "\U0001f600" * 100,  # 100 code points, not 100 bytes
        "é",
        "\N{NO-BREAK SPACE}a",  # a no-break space is not one of the six ASCII whitespace characters
        "a\N{NO-BREAK SPACE}",
        "a\N{EM SPACE}",  # an em space
    ],
)
def test_an_account_name_accepts_valid_names(value) -> None:
    assert TypeAdapter(types.AccountName).validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 101,
        "\U0001f600" * 101,
        " ",
        "\n",
        " a",
        "a ",
        "\ta",
        "a\t",
        "\na",
        "a\n",
        "\x0ba",  # vertical tab
        "a\x0b",
        "\x0ca",  # form feed
        "a\x0c",
        "\ra",
        "a\r",
        "a\x00b",  # a NUL cannot be stored in Postgres text
        "\x00",
        "a\ud800b",  # a lone surrogate cannot be encoded
        5,
        None,
        ["a"],
        b"a",
    ],
)
def test_an_account_name_rejects_everything_else(value) -> None:
    assert not accepts(TypeAdapter(types.AccountName), value)


# --- idempotency keys -------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["k", "key-1", "a key with spaces", "x" * 255, "é"])
def test_an_idempotency_key_accepts_valid_keys(value) -> None:
    assert TypeAdapter(types.IdempotencyKey).validate_python(value) == value


@pytest.mark.parametrize("value", ["", "x" * 256, "a\x00b", "a\ud800b", 5, None, b"k"])
def test_an_idempotency_key_rejects_everything_else(value) -> None:
    assert not accepts(TypeAdapter(types.IdempotencyKey), value)


@pytest.fixture
def header_client() -> TestClient:
    from ledger.api.rest.error_handlers import install_error_handling

    app = FastAPI()
    install_error_handling(app)

    @app.post("/echo")
    def echo(key: Annotated[types.IdempotencyKey, Header(alias="Idempotency-Key")]) -> dict:
        return {"key": key}

    return TestClient(app, raise_server_exceptions=False)


def test_the_idempotency_key_header_is_read_by_that_name(header_client) -> None:
    response = header_client.post("/echo", headers={"Idempotency-Key": "key-1"})
    assert response.status_code == 200
    assert response.json() == {"key": "key-1"}


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 256}])
def test_a_missing_or_out_of_range_key_header_is_a_400_naming_the_header(
    header_client, headers
) -> None:
    response = header_client.post("/echo", headers=headers)
    assert response.status_code == 400
    assert response.json()["errors"][0]["parameter"] == "Idempotency-Key"
