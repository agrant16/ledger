from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from starlette import status

from ledger.accounts import create_account, get_balance
from ledger.api.types import AccountIdInPath, AccountName, Currency
from ledger.db import run_in_transaction


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: AccountName
    currency: Currency


router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountResponse(BaseModel):
    id: int
    account_name: str
    currency: str
    allow_negative: bool


class BalanceResponse(BaseModel):
    account_id: int
    currency: str
    balance_minor: int


@router.post("", status_code=201, response_model=AccountResponse, summary="Create a new account")
def create_new_account(body: CreateAccountRequest, request: Request):
    account = run_in_transaction(
        request.app.state.pool,
        lambda conn: create_account(conn, body.account_name, body.currency, False),
    )
    return AccountResponse(
        id=account.id,
        account_name=account.account_name,
        currency=account.currency,
        allow_negative=account.allow_negative,
    )


@router.get(
    "/{account_id}/balance",
    response_model=BalanceResponse,
    summary="Read an account's cached balance",
    description=(
        "Returns the account's cached balance in minor units. It is read with a single "
        "lock-free statement and reflects every transfer committed when the read ran, so it "
        "may not include a transfer still in flight. It never shows part of a transfer, "
        "because a transfer's entries and balance update commit together."
    ),
)
def get_account_balance(account_id: AccountIdInPath, request: Request):
    balance = run_in_transaction(
        request.app.state.pool, lambda conn: get_balance(conn, account_id), read_only=True
    )
    return BalanceResponse(
        account_id=balance.account_id,
        currency=balance.currency,
        balance_minor=balance.balance_minor,
    )
