import logging
from http import HTTPStatus

from fastapi import APIRouter, Depends, Request


from ledger.api.rest.dependencies import get_pool
from ledger.api.rest.schemas.account_schema import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest, EntryPageResponse,
)
from ledger.api.rest.schemas.types import AccountIdInPath, CursorInQuery, LimitInQuery

from ledger.infra.db import run_in_transaction
from ledger.service.account_service import create_account, get_balance, get_entry_history

router = APIRouter()

logger: logging.Logger = logging.getLogger(__name__)

@router.post(
    path="",
    status_code=HTTPStatus.CREATED,
    response_model=AccountResponse,
    summary="Create a new account",
)
def create_new_account(
    body: CreateAccountRequest,
    request: Request,
    pool=Depends(get_pool),  # noqa: B008
):
    account = run_in_transaction(
        pool,
        lambda conn: create_account(conn, body.account_name, body.currency, False),
    )
    logger.info("account created id=%s currency=%s", account.id, account.currency)
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
def get_account_balance(
    account_id: AccountIdInPath,
    request: Request,
    pool=Depends(get_pool),  # noqa: B008
):
    balance = run_in_transaction(pool, lambda conn: get_balance(conn, account_id), read_only=True)
    return BalanceResponse.from_model(balance)


@router.get(path="/{account_id}/entries",
            response_model=EntryPageResponse,
            summary="Read an account's entries"
            )
def read_account_entries(
        account_id: AccountIdInPath,
        request: Request,
        pool=Depends(get_pool), # noqa: B008
        limit: LimitInQuery = 50,
        before_id: CursorInQuery | None = None,
):
    entries = run_in_transaction(
        pool,
        lambda conn: get_entry_history(conn, account_id, before_id, limit))
    return EntryPageResponse.from_model(entries)