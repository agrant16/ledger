from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Request

from ledger.api.rest.dependencies import PoolDep
from ledger.api.rest.idempotency import request_hash
from ledger.api.rest.openapi import problem_responses
from ledger.api.rest.schemas.transfer_schema import TransferRequest, TransferResponse
from ledger.api.rest.schemas.types import IdempotencyKey, TransferIdInPath
from ledger.errors import (
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    ServiceBusyError,
    UnknownAccountError,
    UnknownTransferError,
)
from ledger.infra.db import run_in_transaction
from ledger.models.transfer_model import TransferPayload
from ledger.service.transfer_service import claim_and_post, get_transfer

router = APIRouter()


@router.post(
    path="",
    status_code=HTTPStatus.CREATED,
    response_model=TransferResponse,
    summary="Move money between two accounts",
    description=(
        "Moves `amount_minor` (a positive integer in minor units, such as cents) of one "
        "currency from `from_account_id` to `to_account_id`. The response is the new transfer "
        "with its two entries: a negative one for the source account and a positive one for "
        "the destination. Both accounts must exist, be different, and hold that currency. "
        "An account that is not allowed to go negative cannot be overdrawn: the request is "
        "refused with a 422 and nothing is written.\n\n"
        "The `Idempotency-Key` header is required: 1 to 255 characters, chosen by the client. "
        "Sending the same key with the same method, path and body again is a replay. It "
        "returns 201 with the same body as the first request, rebuilt from the stored "
        "transfer, and moves nothing a second time, even if the source account could no "
        "longer afford it. The same key with a different request is a 409, which means the "
        "client has a bug. A request that fails with a 400, 404 or 422 does not use up its "
        "key, so it can be retried with the same key and is checked again against current "
        "balances. A 503 means the request may not have completed; retrying with the same key is "
        "safe either way."
    ),
    responses=problem_responses(
        InvalidRequestError,
        CurrencyMismatchError,
        UnknownAccountError,
        IdempotencyConflictError,
        InsufficientFundsError,
        ServiceBusyError,
    ),
)
def create_transfer(
    body: TransferRequest,
    idempotency_key: Annotated[IdempotencyKey, Header()],
    request: Request,
    pool: PoolDep,
) -> TransferResponse:
    transfer = run_in_transaction(
        pool,
        lambda conn: claim_and_post(
            conn,
            idempotency_key,
            request_hash(request.method, request.url.path, body),
            TransferPayload(
                from_account_id=body.from_account_id,
                to_account_id=body.to_account_id,
                currency=body.currency,
                amount_minor=body.amount_minor,
            ),
        ),
    )
    return TransferResponse.from_model(transfer)


@router.get(
    path="/{transfer_id}",
    response_model=TransferResponse,
    summary="Read a transfer with its entries",
    description=(
        "Returns a committed transfer: its id, the id of the transfer it reverses (null for a "
        "normal transfer), the time it was created in UTC, and its entries (id, account, "
        "signed amount in minor units, currency) in the order they were written. The body is "
        "exactly what POST /transfers returns, so a replayed request and a later read agree. "
        "A transfer is visible only once it has committed, and always with all of its entries, "
        "never part of one. Account balances are not included, because they change after the "
        "transfer. An unknown id is a 404; an id that is not plain decimal digits is a 400."
    ),
    responses=problem_responses(InvalidRequestError, UnknownTransferError, ServiceBusyError),
)
def get_transfer_by_id(
    transfer_id: TransferIdInPath,
    pool: PoolDep,
) -> TransferResponse:
    transfer = run_in_transaction(
        pool, lambda conn: get_transfer(conn, transfer_id), read_only=True
    )
    return TransferResponse.from_model(transfer)
