from datetime import datetime, timezone
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from ledger.api.rest.schemas.types import AccountId, AmountMinor, Currency
from ledger.models.posting_model import TransferEntry, Transfer


class TransferRequest(BaseModel):
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

    @classmethod
    def from_model(cls, entry: TransferEntry) -> Self:
        return cls(
            id = entry.id,
            account_id = entry.account_id,
            amount_minor = entry.amount_minor,
            currency = entry.currency,
        )


class TransferResponse(BaseModel):
    id: int
    reverses_transaction_id: int
    created_at: str
    entries: list[TransferEntryResponse]

    @classmethod
    def from_model(cls, transfer: Transfer) -> Self:
        return cls(
            id = transfer.id,
            reverses_transaction_id = transfer.reverses_transaction_id,
            created_at = transfer.created_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            entries = [TransferEntryResponse.from_model(entry) for entry in transfer.entries],
        )
