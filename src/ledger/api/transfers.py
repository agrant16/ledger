from pydantic import BaseModel, ConfigDict

from ledger.api.types import AccountId, AmountMinor, Currency


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account: AccountId
    to_account: AccountId
    amount: AmountMinor
    currency: Currency
