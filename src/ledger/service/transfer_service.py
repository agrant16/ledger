import psycopg

from ledger.errors import LedgerInvariantError, UnknownTransferError
from ledger.models.posting_model import Entry
from ledger.models.transfer_model import Transfer
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method


def entries_for_transfer(
    from_account_id: int, to_account_id: int, amount_minor: int, currency: str
) -> list[Entry]:
    """Builds the two entries of a transfer: a debit on the sender, a credit on the receiver.

    The amounts are equal and opposite, so the entries sum to zero. (rule 1)

    Args:
        from_account_id: The account debited.
        to_account_id: The account credited.
        amount_minor: The amount, in minor units.
        currency: The currency of the transfer.

    Returns:
        Two entries, the debit first, that sum to zero.
    """
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
    """Returns a transfer with its entries.

    Args:
        conn: The request's connection, inside the caller's transaction.
        transfer_id: The id of the transaction to read.

    Returns:
        The transfer, with its entries ordered by entry id.

    Raises:
        UnknownTransferError: No transaction has this id.
        LedgerInvariantError: The transaction exists but has no entries.
    """
    transfer = posting_repo.find_transfer(conn, transfer_id)
    if transfer is None:
        raise UnknownTransferError(f"Transfer {transfer_id} not found")
    if not transfer.entries:
        raise LedgerInvariantError(f"Critical error: transfer {transfer_id} has no entry records")
    return transfer
