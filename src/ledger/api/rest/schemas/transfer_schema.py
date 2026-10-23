from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from ledger.api.rest.schemas.types import AccountId, AmountMinor, Currency, timestamp_converter
from ledger.models.transfer_model import Transfer


class TransferRequest(BaseModel):
    """A request to move money from one account to another.

    The two accounts must be different.
    """

    model_config = ConfigDict(extra="forbid")
    from_account_id: AccountId
    to_account_id: AccountId
    amount_minor: AmountMinor
    currency: Currency

    @model_validator(mode="after")
    def accounts_differ(self):
        if self.from_account_id == self.to_account_id:
            raise ValueError("from and to must be different accounts")
        return self


class TransferEntryResponse(BaseModel):
    id: int
    account_id: int
    amount_minor: int
    currency: str


class TransferResponse(BaseModel):
    id: int
    reverses_transaction_id: int | None
    created_at: str
    entries: list[TransferEntryResponse]

    @classmethod
    def from_model(cls, transfer: Transfer) -> Self:
        """Builds the response from a transfer.

        Args:
            transfer: The transfer, with its entries.

        Returns:
            The response, with the timestamp as an ISO 8601 UTC string.
        """
        return cls(
            id=transfer.id,
            reverses_transaction_id=transfer.reverses_transaction_id,
            created_at=timestamp_converter(transfer.created_at),
            entries=[
                TransferEntryResponse(
                    id=entry.id,
                    account_id=entry.account_id,
                    amount_minor=entry.amount_minor,
                    currency=entry.currency,
                )
                for entry in transfer.entries
            ],
        )
