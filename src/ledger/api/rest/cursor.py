import base64
import re

from ledger.api.rest.schemas.types import POSTGRES_BIG_INT
from ledger.errors import InvalidRequestError

BASE64URL_REGEX = re.compile(r'^[A-Za-z0-9_-]+={0,2}$')

def _validate_encoded_str(encoded_str: str) -> None:
    """Validates that the passed string is a base64 encoded string."""
    if len(encoded_str) % 4 == 1 or not bool(BASE64URL_REGEX.match(encoded_str)):
        raise InvalidRequestError(f"Invalid cursor value: passed value is not a base64url encoded string")

def _validate_id_is_in_bounds(entry_id: int) -> None:
    """Entry ids must be between 1 and 922337203685477580"""
    if entry_id < 1 or entry_id > POSTGRES_BIG_INT:
        raise InvalidRequestError(
            f"Invalid cursor value: decoded value must be between 1 and 9223372036854775807"
        )

def _validate_decoded_value(decoded_str: str, encoded_str: str) -> None:
    """
    Validates that the decoded string is a base 10 integer value. Calls _validate_id_is_in_bounds
    to validate the integer is in the required range.
    """
    try:
        int(decoded_str, base=10)
    except ValueError:
        raise InvalidRequestError(f"Invalid cursor value: value does not decode to an integer")
    _validate_id_is_in_bounds(int(decoded_str, base=10))

def encode_cursor(entry_id: int) -> str:
    """Encodes an integer as a base64 encoded string and strips padding."""
    _validate_id_is_in_bounds(entry_id)
    return base64.urlsafe_b64encode(str(entry_id).encode("utf-8")).decode("utf-8").rstrip("=")


def decode_cursor(encoded_str: str) -> int:
    """
    Decodes a base64 encoded string back to an integer value.

    Raises an InvalidRequestError
    """
    try:
        _validate_encoded_str(encoded_str)
        decoded_str = base64.urlsafe_b64decode(encoded_str + "===").decode("utf-8")
        _validate_decoded_value(decoded_str, encoded_str)
        num = int(decoded_str, base=10)
        if encode_cursor(num) != encoded_str:
            raise InvalidRequestError(f"Invalid cursor value: passed value does not decode and encode back to itself")
        return num
    except ValueError as e:
        raise InvalidRequestError(f"Invalid decode operation: {str(e)}")