from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: int
    account_name: str
    currency: str
    allow_negative: bool


@dataclass(frozen=True)
class Balance:
    account_id: int
    currency: str
    balance_minor: int
