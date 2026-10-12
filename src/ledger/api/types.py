import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field

ASCII_WHITESPACE = " \t\n\v\f\r"

POSTGRES_BIG_INT = 9223372036854775807

_CANONICAL_ID = re.compile(r"[1-9][0-9]*")

def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _not_padded(value: str) -> str:
    if value != value.strip(ASCII_WHITESPACE):
        raise ValueError("must not start or end with whitespace")
    return value


def _account_id_range(value: int) -> int:
    if not 1 <= value <= POSTGRES_BIG_INT:
        raise ValueError("ID must be between 1 and 9,223,372,036,854,775,807")
    return value


def _canonical_digits_to_int(value: object) -> int:
    if not isinstance(value, str) or len(value) > 19 or not _CANONICAL_ID.fullmatch(value):
        raise ValueError("must be written as plain decimal digits, without a sign or leading zero")
    return int(value)

AccountId = Annotated[
    int,
    Field(
        strict=True,
        ge=1,
        le=POSTGRES_BIG_INT,
        description=f"A strict integer from 1 to {POSTGRES_BIG_INT}",
    ),
]

AccountIdInPath = Annotated[
    int,
    BeforeValidator(_canonical_digits_to_int),
    Field(ge=1, le=POSTGRES_BIG_INT, description=f"A strict integer from 1 to {POSTGRES_BIG_INT}"),
]

AmountMinor = Annotated[
    int,
    Field(
        strict=True, ge=1, le=99900000000, description="A strict integer from 1 to 99,900,000,000"
    ),
]

Currency = Annotated[
    str,
    Field(
        strict=True,
        pattern=r"^[A-Z]{3}$",
        description="A string of exactly three uppercase ASCII letters",
    ),
]

AccountName = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=100,
        description="A string with 1 to 100 characters (code points); "
        "no leading or trailing space, tab, newline, vertical tab, form "
        "feed or carriage return; no NUL; no surrogates",
    ),
    AfterValidator(_no_nul),
    AfterValidator(_not_padded),
]

IdempotencyKey = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=255,
        description="A string with 1 to 255 characters; no NUL; no surrogates",
    ),
    AfterValidator(_no_nul),
]
