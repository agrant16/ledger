from typing import Self

from pydantic import BaseModel, ConfigDict

from ledger.api.rest.cursor import encode_cursor
from ledger.api.rest.schemas.types import AccountName, Currency, timestamp_converter
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
        """Builds the response from a page of entries. (ADR 0014)

        Args:
            entry_page: The page, in the order the repository returned it.

        Returns:
            The response. `next_cursor` is `None` when there are no more entries, otherwise the
            cursor for the entry after the last one in the page.
        """
        cursor = encode_cursor(entry_page.entries[-1].id) if entry_page.has_more else None
        return cls(
            entries=[
                EntryResponse(
                    id=entry.id,
                    transaction_id=entry.transaction_id,
                    account_id=entry.account_id,
                    amount_minor=entry.amount_minor,
                    currency=entry.currency,
                    created_at=timestamp_converter(entry.created_at),
                )
                for entry in entry_page.entries
            ],
            next_cursor=cursor,
        )
