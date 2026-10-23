import hashlib
import json

from pydantic import BaseModel


def request_hash(method: str, path: str, body: BaseModel | None) -> str:
    """Returns the hash that tells a retry of a request from a reuse of its idempotency key.

    The form is a contract (rule 5, ADR 0012). A stored hash is compared with every new one, so
    any change to the form makes stored keys look like conflicts and needs a migration plan and
    a new ADR.

    The result is the SHA-256, as 64 lowercase hex digits, of the canonical JSON of
    `[METHOD, path, body]`: keys sorted, no spaces, the method uppercased, and the body as the
    validated model dumped to JSON, so a default written out hashes the same as one left out.
    Text is not normalized.

    Args:
        method: The HTTP method. Case does not matter.
        path: The path the request was routed on.
        body: The validated request model, or `None` when the request has no body.

    Returns:
        The 64-character lowercase hex digest.
    """
    hash_payload = [method.upper(), path, None if body is None else body.model_dump(mode="json")]
    canonical = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
