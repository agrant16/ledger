from typing import Self

from pydantic import BaseModel, ConfigDict

from ledger.api.rest.schemas.types import AccountName, Currency
from ledger.models.account_model import Account, Balance


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
