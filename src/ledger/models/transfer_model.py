from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TransferEntry:
    """Represents one side of a transfer.

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
    """The transfer and it's associated entries.

    Attributes:
        id (int): The id of the transfer.
        reverses_transaction_id (int | None): The id of the transaction this transfer reversed.
        created_at (datetime): The date and time the transfer was created.
        entries (list[TransferEntry]): The entries associated with this transfer.
    """

    id: int
    reverses_transaction_id: int | None
    created_at: datetime
    entries: tuple[TransferEntry, ...]
