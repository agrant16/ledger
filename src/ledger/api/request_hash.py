import hashlib
import json

from ledger.api.transfers import TransferRequest


def request_hash(method: str, path: str, body: TransferRequest | None) -> str:
    hash_payload = [method.upper(), path, None if body is None else body.model_dump(mode="json")]
    canonical = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
