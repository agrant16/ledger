from datetime import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Claim:
    transaction_id: int
    replay: bool


@dataclass(frozen=True)
class LockedAccount:
    currency: str
    allow_negative: bool
    balance_minor: int


@dataclass(frozen=True)
class TransferEntry:
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Transfer:
    transfer_id: int
    reverses_transaction_id: int
    created_at: datetime
    entries: tuple[TransferEntry, ...]




    