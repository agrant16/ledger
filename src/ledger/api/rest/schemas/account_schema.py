from datetime import datetime, UTC
from typing import Self

from pydantic import BaseModel, ConfigDict

from ledger.api.rest.cursor import encode_cursor
from ledger.api.rest.schemas.types import AccountName, Currency
from ledger.models.account_model import Account, Balance
from ledger.models.entry_history_model import EntryPage


class AccountResponse(BaseModel):
    id: int
    account_name: str
    currency: str
    allow_negative: bool

    @classmethod
    def from_model(cls, account: Account) -> Self:
        return cls(
            id=account.id,
            account_name=account.account_name,
            currency=account.currency,
            allow_negative=account.allow_negative,
        )


class BalanceResponse(BaseModel):
    account_id: int
    currency: str
    balance_minor: int

    @classmethod
    def from_model(cls, balance: Balance) -> Self:
        return cls(
            account_id=balance.account_id,
            currency=balance.currency,
            balance_minor=balance.balance_minor,
        )


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: AccountName
    currency: Currency


class EntryResponse(BaseModel):
    id: int
    transaction_id: int
    account_id: int
    amount_minor: int
    currency: str
    created_at: str


class EntryPageResponse(BaseModel):
    entries: list[EntryResponse]
    next_cursor: str | None
    @classmethod
    def from_model(cls, entry_page: EntryPage) -> Self:
        cursor = encode_cursor(entry_page.entries[-1].id) if EntryPage.has_more else None
        entries = entry_page.entries[:-1] if EntryPage.has_more else entry_page.entries
        return cls(
            entries=[EntryResponse(
                id = entry.id,
                transaction_id=entry.transaction_id,
                account_id=entry.account_id,
                amount_minor=entry.amount_minor,
                currency=entry.currency,
                created_at=entry.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
            for entry in entries],
            next_cursor=cursor
        )