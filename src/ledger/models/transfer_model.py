from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TransferEntry:
    id: int
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Transfer:
    id: int
    reverses_transaction_id: int | None
    created_at: datetime
    entries: tuple[TransferEntry, ...]
