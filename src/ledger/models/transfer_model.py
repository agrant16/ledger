from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TransferEntry:
    """One side of a transfer.

    Attributes:
        id (int): The id of the entry.
        account_id (int): The account id of the entry.
        amount_minor (int): The amount of the money movement.
        currency (str): The currency of the money movement.
    """

    id: int
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Transfer:
    """A transfer and its entries, in the order they were written.

    Attributes:
        id (int): The id of the transaction.
        reverses_transaction_id (int | None): The id of the transaction this one reverses,
            or `None` for a normal transfer.
        created_at (datetime): When the transaction was created.
        entries (tuple[TransferEntry, ...]): The transfer's entries.
    """

    id: int
    reverses_transaction_id: int | None
    created_at: datetime
    entries: tuple[TransferEntry, ...]


@dataclass(frozen=True)
class TransferPayload:

    from_account_id: int
    to_account_id: int
    currency: str
    amount_minor: int
