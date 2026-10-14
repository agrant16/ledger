from pydantic import BaseModel, ConfigDict, model_validator

from ledger.api.rest.schemas.types import AccountId, AmountMinor, Currency


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
