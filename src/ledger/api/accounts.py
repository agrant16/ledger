from fastapi import APIRouter, Request

from ledger.api.rest.schemas.account_schema import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest,
)
from ledger.api.rest.schemas.types import AccountIdInPath
from ledger.infra.db import run_in_transaction
from ledger.service.account_service import create_account, get_balance

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", status_code=201, response_model=AccountResponse, summary="Create a new account")
def create_new_account(body: CreateAccountRequest, request: Request):
    account = run_in_transaction(
        request.app.state.pool,
        lambda conn: create_account(conn, body.account_name, body.currency, False),
    )
    return AccountResponse.from_model(account)


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
    return BalanceResponse.from_model(balance)