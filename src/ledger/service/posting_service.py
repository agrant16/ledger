from collections import defaultdict

import psycopg

from ledger.errors import (
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientFundsError,
    LedgerInvariantError,
    UnbalancedEntriesError,
    UnknownAccountError,
)
from ledger.models.posting_model import Claim, Entry
from ledger.models.transfer_model import Transfer, TransferPayload
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method
from ledger.service.transfer_service import entries_for_transfer


@transactional_service_method
def claim_transaction(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> Claim:
    """Claims an idempotency key, or recognizes a repeat of an earlier claim. (rule 5, ADR 0012)

    A new key inserts the transactions row and returns its id with `replay=False`.
    A key that already exists returns the original id with `replay=True` when the
    request hash matches, and writes nothing.

    Args:
        conn: The request's connection, inside the caller's transaction.
        idempotency_key: The client's Idempotency-Key.
        request_hash: The canonical hash of the request.
        reverses_transaction_id: The transaction this one reverses, if any.

    Returns:
        The transaction id, and whether it is a replay.

    Raises:
        IdempotencyConflictError: The key was already used with a different request.
        LedgerInvariantError: The key conflicted but its row cannot be found or a
            repository-level data integrity constraint is violated.
    """
    transaction_id = posting_repo.insert_claim(
        conn, idempotency_key, request_hash, reverses_transaction_id
    )
    if transaction_id is None:
        existing_record = posting_repo.find_claim(conn, idempotency_key)
        if not existing_record:
            raise LedgerInvariantError(
                "Critical error in claim_transaction: row should exist but can't be found"
            )
        existing_id, existing_hash = existing_record
        if request_hash == existing_hash:
            return Claim(transaction_id=existing_id, replay=True)
        raise IdempotencyConflictError("Idempotency key was already used with a different request")
    return Claim(transaction_id=transaction_id, replay=False)


@transactional_service_method
def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    """
    Validates and writes the entries of a claimed transaction. (rules 1, 4, 6, ADR 0005, ADR 0006)

    Nothing is written unless every check passes, so a rejected post leaves no
    entries and no balance change. An account that may not go negative stays at
    or above zero under concurrent posts. This function never commits; on an
    error the caller's rollback also removes the claim.

    Checks, in order:
    1. entries are present
    2. they sum to zero per currency (rule 1)
    3. every account named exists
    4. each entry's currency matches its account (rule 6)
    5. no account with `allow_negative` false would end below zero once all the
       entries are applied (rule 4)

    Args:
        conn: The request's connection, inside the caller's transaction.
        transaction_id: The id returned by `claim_transaction`.
        entries: The entries to write; negative amounts debit, positive credit.

    Raises:
        UnbalancedEntriesError: The entries do not sum to zero in some currency.
        UnknownAccountError: An entry names an account that does not exist.
        CurrencyMismatchError: An entry's currency differs from its account's.
        InsufficientFundsError: An account that may not go negative would.
        LedgerInvariantError: The entry list is empty, the database fails to update every
            target account balance, or a repository-level data integrity constraint is
            violated.
    """
    if not entries:
        raise LedgerInvariantError(f"No entries for transaction {transaction_id}")

    # check that entries for each currency sum to 0
    totals: dict[str, int] = defaultdict(int)
    for entry in entries:
        totals[entry.currency] += entry.amount_minor
    if any(total != 0 for total in totals.values()):
        raise UnbalancedEntriesError(f"Entries for transaction {transaction_id} are not balanced")

    deltas: dict[int, int] = defaultdict(int)
    for entry in entries:
        deltas[entry.account_id] += entry.amount_minor

    # lock accounts in entries and check for unknown accounts
    locked_accounts = posting_repo.lock_accounts(conn, list(deltas.keys()))
    if len(locked_accounts) != len(deltas.keys()):
        missing = sorted(deltas.keys() - locked_accounts.keys())
        raise UnknownAccountError(
            f"Entries for transaction {transaction_id} include unknown accounts: {missing}"
        )

    # check that accounts in entries have matching currencies with the accounts table.
    for entry in entries:
        if locked_accounts[entry.account_id].currency != entry.currency:
            raise CurrencyMismatchError(
                f"Currency mismatch for account {entry.account_id} on transaction {transaction_id}"
            )

    # check that from account has enough balance to cover transactions
    # uses the same result from locking statement earlier which keeps
    # rows from being updated by other transactions
    for account_id, delta in sorted(deltas.items()):
        account = locked_accounts[account_id]
        if not account.allow_negative and account.balance_minor + delta < 0:
            raise InsufficientFundsError(account_id, account.balance_minor, delta)

    posting_repo.insert_entries(conn, transaction_id, entries)
    updated_accounts = posting_repo.apply_balance_deltas(conn, deltas)

    if len(updated_accounts) != len(deltas.keys()):
        missing_id = sorted(deltas.keys() - updated_accounts)
        raise LedgerInvariantError(
            f"Critical Error: Not all balances were updated for account {missing_id}"
        )


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
    if not transfer.entries:
        raise LedgerInvariantError(
            f"Critical error: transfer {claim.transaction_id} has no entries."
        )
    return transfer
