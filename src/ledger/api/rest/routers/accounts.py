import logging
from http import HTTPStatus

from fastapi import APIRouter, Depends, Request

from ledger.api.rest.dependencies import get_pool
from ledger.api.rest.openapi import problem_responses
from ledger.api.rest.schemas.account_schema import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest,
    EntryPageResponse,
)
from ledger.api.rest.schemas.types import AccountIdInPath, CursorInQuery, LimitInQuery
from ledger.errors import (
    DuplicateAccountNameError,
    InvalidRequestError,
    ServiceBusyError,
    UnknownAccountError,
)
from ledger.infra.db import run_in_transaction
from ledger.models.entry_history_model import DEFAULT_PAGE_SIZE
from ledger.service.account_service import create_account, get_balance, get_entry_history

router = APIRouter()

logger: logging.Logger = logging.getLogger(__name__)


@router.post(
    path="",
    status_code=HTTPStatus.CREATED,
    response_model=AccountResponse,
    summary="Create a new account",
    description=(
        "Creates an account from `account_name` (1 to 100 characters, no leading or trailing "
        "whitespace, unique and case-sensitive) and `currency` (three uppercase letters). Every "
        "account created here is protected: it cannot go below zero, and the request cannot say "
        "otherwise (`allow_negative` is rejected as an unknown field). The balance starts at 0 "
        "and is read through the balance endpoint. A name that is already taken is a 409. "
        "There is no idempotency key, so retrying after a lost response returns that 409."
    ),
    responses=problem_responses(InvalidRequestError, DuplicateAccountNameError, ServiceBusyError),
)
def create_new_account(
    body: CreateAccountRequest,
    pool=Depends(get_pool),  # noqa: B008
) -> AccountResponse:
    account = run_in_transaction(
        pool,
        lambda conn: create_account(conn, body.account_name, body.currency, allow_negative=False),
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
        "because a transfer's entries and balance update commit together. An unknown account "
        "is a 404; an id that is not plain decimal digits is a 400."
    ),
    responses=problem_responses(InvalidRequestError, UnknownAccountError, ServiceBusyError),
)
def get_account_balance(
    account_id: AccountIdInPath,
    pool=Depends(get_pool),  # noqa: B008
) ->BalanceResponse:
    balance = run_in_transaction(pool, lambda conn: get_balance(conn, account_id), read_only=True)
    return BalanceResponse.from_model(balance)


@router.get(
    path="/{account_id}/entries",
    response_model=EntryPageResponse,
    summary="Read an account's entry history, newest first",
    description=(
        "Returns a page of the account's entries, newest first by entry id. Each entry has "
        "its id, `transaction_id`, `account_id`, signed `amount_minor`, `currency`, and "
        "`created_at`, the time of the transaction it belongs to (UTC). `created_at` is "
        "informational: entries are ordered by id, and a transfer that started earlier can "
        "commit later, so timestamps are not strictly in id order.\n\n"
        "`limit` is the page size, from 1 to 200 (default 50). A value outside that range, "
        "or one that is not plain decimal digits, is a 400 and is never clamped. To get the "
        "next page, pass the `next_cursor` of the previous response as `cursor`. `next_cursor` "
        "is null on the last page. The cursor is opaque: treat it as a string to pass back "
        "unchanged. A cursor the service did not issue is a 400. There is no total count.\n\n"
        "Paging by cursor, not by offset, means a page boundary never skips or repeats an "
        "entry, even while new transfers are committed: new entries have higher ids than "
        "any page already read, so they appear only at the start of a fresh walk. An unknown "
        "account is a 404; an account with no entries returns an empty page."
    ),
    responses=problem_responses(InvalidRequestError, UnknownAccountError, ServiceBusyError),
)
def read_account_entries(
    account_id: AccountIdInPath,
    pool=Depends(get_pool),  # noqa: B008
    limit: LimitInQuery = DEFAULT_PAGE_SIZE,
    cursor: CursorInQuery | None = None,
) -> EntryPageResponse:
    entries = run_in_transaction(
        pool, lambda conn: get_entry_history(conn, account_id, cursor, limit), read_only=True
    )
    return EntryPageResponse.from_model(entries)
