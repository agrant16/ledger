import psycopg

from ledger.errors import LedgerInvariantError, UnknownTransferError
from ledger.models.posting_model import Entry
from ledger.models.transfer_model import Transfer
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method


def entries_for_transfer(
    from_account_id: int, to_account_id: int, amount_minor: int, currency: str
) -> list[Entry]:
    return [
        Entry(
            account_id=from_account_id,
            amount_minor=-amount_minor,
            currency=currency,
        ),
        Entry(
            account_id=to_account_id,
            amount_minor=amount_minor,
            currency=currency,
        ),
    ]


@transactional_service_method
def get_transfer(conn: psycopg.Connection, transfer_id: int) -> Transfer:
    transfer = posting_repo.find_transfer(conn, transfer_id)
    if transfer is None:
        raise UnknownTransferError(f"Transfer {transfer_id} not found")
    if not transfer.entries:
        raise LedgerInvariantError(f"Critical error: transfer {transfer_id} has no entry records")
    return transfer
