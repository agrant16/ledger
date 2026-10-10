from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    currency: str
    allow_negative: bool
