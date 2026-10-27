import base64
import re

from ledger.errors import InvalidRequestError, LedgerInvariantError

_BASE64URL_REGEX = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

POSTGRES_BIG_INT = 9223372036854775807


def _validate_encoded_str(encoded_str: str) -> None:
    """Validates that the passed string is a base64 encoded string."""
    if len(encoded_str) % 4 == 1 or not _BASE64URL_REGEX.fullmatch(encoded_str):
        raise InvalidRequestError(
            "Invalid cursor value: passed value is not a base64url encoded string"
        )


def _in_bounds(entry_id: int) -> bool:
    return 0 < entry_id <= POSTGRES_BIG_INT


def encode_cursor(entry_id: int) -> str:
    """Encodes an entry id as an opaque cursor. (ADR 0014)

    The cursor is the id's decimal text in base64url, without `=` padding.

    Args:
        entry_id: The id of the last entry on the page.

    Returns:
        The cursor.

    Raises:
        LedgerInvariantError: The id is outside 1 to 9223372036854775807, which is a bug
            and not a bad request.
    """
    if not _in_bounds(entry_id):
        raise LedgerInvariantError("Critical error: entry_id is out of range")
    return base64.urlsafe_b64encode(str(entry_id).encode()).decode("ascii").rstrip("=")


def decode_cursor(encoded_str: str) -> int:
    """Decodes a cursor back to the entry id it holds. (ADR 0014)

    Args:
        encoded_str: The cursor from the client.

    Returns:
        The entry id.

    Raises:
        InvalidRequestError: The cursor is not base64url, does not hold a decimal integer,
            holds an id outside 1 to 9223372036854775807, or is not the exact text
            `encode_cursor` produces for that id.
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
