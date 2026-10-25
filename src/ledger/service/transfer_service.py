import psycopg

from ledger.errors import LedgerInvariantError, UnknownTransferError
from ledger.models.posting_model import Entry
from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method
from ledger.service.posting_service import claim_transaction, post_transaction


def _empty_entries_check(transfer: Transfer) -> None:
    if not transfer.entries:
        raise LedgerInvariantError(f"Critical error: transfer {transfer.id} has no entries.")


def entries_for_transfer(payload: TransferPayload) -> list[Entry]:
    """Builds the two entries of a transfer: a debit on the sender, a credit on the receiver.

    The amounts are equal and opposite, so the entries sum to zero. (rule 1)

    Args:
        payload: The transfer information.

    Returns:
        Two entries, the debit first, that sum to zero.
    """
    return [
        Entry(
            account_id=payload.from_account_id,
            amount_minor=-payload.amount_minor,
            currency=payload.currency,
        ),
        Entry(
            account_id=payload.to_account_id,
            amount_minor=payload.amount_minor,
            currency=payload.currency,
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
    _empty_entries_check(transfer)
    return transfer


@transactional_service_method
def claim_and_post(
    conn: psycopg.Connection, idempotency_key: str, request_hash: str, payload: TransferPayload
) -> Transfer:
    """Claims the key and posts a two-entry transfer. (rule 5, ADR 0005)

    On a replay the post is skipped and the stored transfer is returned, so the
    money moves once. The claim and the posting share the request's transaction:
    if the post fails, the rollback removes the claim too and the key is not used up.

    Args:
        conn: The request's connection, inside the caller's transaction.
        idempotency_key: The client's Idempotency-Key.
        request_hash: The canonical hash of the request.
        payload: The transfer information.

    Returns:
        The transfer, newly posted or replayed, with its entries.

    Raises:
        IdempotencyConflictError: The key was already used with a different request.
        UnknownAccountError: The debit or credit account does not exist.
        CurrencyMismatchError: The transfer currency differs from an account's currency.
        InsufficientFundsError: The debit account cannot cover the transfer amount.
        LedgerInvariantError: The created transfer or entries cannot be found, an underlying
            structural invariant is violated, or a repository-level data integrity constraint
            is violated.
    """
    claim = claim_transaction(conn, idempotency_key, request_hash)
    if not claim.replay:
        post_transaction(
            conn,
            claim.transaction_id,
            entries_for_transfer(payload),
        )
    transfer = posting_repo.find_transfer(conn, claim.transaction_id)
    if transfer is None:
        raise LedgerInvariantError(
            f"Critical error: transfer {claim.transaction_id} cannot be found."
        )
    _empty_entries_check(transfer)
    return transfer
