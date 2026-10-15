from fastapi import APIRouter, Depends, Request

from ledger.api.rest.dependencies import get_pool
from ledger.api.rest.schemas.transfer_schema import TransferResponse
from ledger.api.rest.schemas.types import TransferIdInPath
from ledger.infra.db import run_in_transaction
from ledger.service.posting_service import get_transfer

router = APIRouter()


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
)
def get_transfer_by_id(
    transfer_id: TransferIdInPath,
    request: Request,
    pool=Depends(get_pool),  # noqa: B008
) -> TransferResponse:
    transfer = run_in_transaction(pool, lambda conn: get_transfer(conn, transfer_id))
    return TransferResponse.from_model(transfer)
