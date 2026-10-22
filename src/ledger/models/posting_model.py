from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    """One side of a money movement. A sender entry has a negative amount_minor (credit).
     A receiver entry has a positive amount_minor (debit).

    Attributes:
        account_id (int): The account id associated with the entry.
        amount_minor (int): The amount of the money movement.
        currency (str): The currency of the money movement.
    """

    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Claim:
    """
    A claim is the result of claiming a key for the transfer.

    Attributes:
        transaction_id (int): The transaction id associated with the claim.
        replay (bool): Whether the claim is replay or not (the key has already been claimed).
    """

    transaction_id: int
    replay: bool


@dataclass(frozen=True)
class LockedAccount:
    """A simple account model used to store balance information while the account
    records are locked for the balance table update.

    Attributes:
        currency (str): The currency of the money movement.
        allow_negative (bool): Whether the account is protected (allow_negative=False) or not.
        balance_minor (int): The balance amount of the locked account."""

    currency: str
    allow_negative: bool
    balance_minor: int
