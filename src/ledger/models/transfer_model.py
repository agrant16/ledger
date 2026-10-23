from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TransferEntry:
    """One side of a transfer.

    Attributes:
        id: The id of the entry.
        account_id: The account id of the entry.
        amount_minor: The amount of the money movement, in minor units.
        currency: The currency of the money movement.
    """

    id: int
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Transfer:
    """A transfer and its entries, in the order they were written.

    Attributes:
        id: The id of the transaction.
        reverses_transaction_id: The id of the transaction this one reverses,
            or `None` for a normal transfer.
        created_at: When the transaction was created.
        entries: The transfer's entries.
    """

    id: int
    reverses_transaction_id: int | None
    created_at: datetime
    entries: tuple[TransferEntry, ...]


@dataclass(frozen=True)
class TransferPayload:
    """Transfer information from a POST /transfers call.

    Attributes:
        from_account_id: The account id being debited.
        to_account_id: The account id being credited.
        amount_minor: The amount of the money movement, in minor units.
        currency: The currency of the money movement.
    """

    from_account_id: int
    to_account_id: int
    amount_minor: int
    currency: str
