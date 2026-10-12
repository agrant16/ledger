from pydantic import BaseModel, ConfigDict

from ledger.api.types import AccountName, Currency


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: AccountName
    currency: Currency
