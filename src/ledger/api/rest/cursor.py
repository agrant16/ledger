import base64
import re

from ledger.errors import InvalidRequestError, LedgerInvariantError

_BASE64URL_REGEX = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

POSTGRES_BIG_INT = 9223372036854775807

def _validate_encoded_str(encoded_str: str) -> None:
    """Validates that the passed string is a base64 encoded string."""
    if len(encoded_str) % 4 == 1 or not bool(_BASE64URL_REGEX.fullmatch(encoded_str)):
        raise InvalidRequestError(
            "Invalid cursor value: passed value is not a base64url encoded string"
        )


def _in_bounds(entry_id: int) -> bool:
    """Entry ids must be integers between 1 and 9223372036854775807"""
    if not 0 < entry_id <= POSTGRES_BIG_INT:
        return False
    return True


def encode_cursor(entry_id: int) -> str:
    """
    Encodes an integer as a base64 encoded string and strips padding.

    Raises a LedgerInvariantError instead of InvalidRequestError. encode_cursor is directly
    called within the application to encode an entry id from the entries database. if the
    value is outside the allowed range it is a bug and not a bad request.
    """
    if not _in_bounds(entry_id):
        raise LedgerInvariantError("Critical error: entry_id is out of range")
    return base64.urlsafe_b64encode(str(entry_id).encode()).decode("ascii").rstrip("=")


def decode_cursor(encoded_str: str) -> int:
    """
    Decodes a base64 encoded string back to an integer value.

    Raises an InvalidRequestError on any error. ValueError is caught as
    it allows the specific InvalidRequestErrors to be raised and passthrough.
    It catches the other possible errors (binascii.Error, UnicodeDecodeError, and int() failures)
    and converts to an InvalidRequestError.
    """
    _validate_encoded_str(encoded_str)

    try:
        decoded_str = base64.urlsafe_b64decode(encoded_str + "===").decode("ascii")
        entry_id = int(decoded_str, base=10)
    except ValueError:
        raise InvalidRequestError("Invalid cursor decode operation") from None

    if not _in_bounds(entry_id):
        raise InvalidRequestError(
            "Invalid cursor value: must be an integer between 1 and 9223372036854775807"
        )

    if encode_cursor(entry_id) != encoded_str:
        raise InvalidRequestError(
            "Invalid cursor value: passed value does not decode and encode back to itself"
        )

    return entry_id
