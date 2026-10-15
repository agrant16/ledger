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
