from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    """One side of a money movement.

    A sender entry has a negative `amount_minor` and a receiver entry has a positive one.

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
    """The result of claiming an idempotency key.

    Attributes:
        transaction_id (int): The id of the transaction row for the key.
        replay (bool): Whether the key was already claimed by an earlier identical request.
    """

    transaction_id: int
    replay: bool


@dataclass(frozen=True)
class LockedAccount:
    """An account's currency, protection flag and locked balance. (rule 4)

    Attributes:
        currency (str): The currency of the account.
        allow_negative (bool): Whether the account may go negative (a funding account).
        balance_minor (int): The balance returned by the locking `SELECT ... FOR UPDATE`.
    """

    currency: str
    allow_negative: bool
    balance_minor: int
