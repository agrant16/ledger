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
from ledger.models.transfer_model import Transfer
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method
from ledger.service.transfer_service import entries_for_transfer, get_transfer


@transactional_service_method
def claim_transaction(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> Claim:
    """Claim an idempotency key, or recognise a repeat of an earlier claim. (rule 5)

    A new key inserts the transactions row and returns its id with
    replay=False. If the key already exists, the stored request hash decides:
    the same hash is a replay of the original request and returns the original
    id with replay=True, and nothing is written. A different hash means the key
    was reused for another request and raises IdempotencyConflictError. The
    stored row is read in a second statement because the app role cannot
    UPDATE transactions. (ADR 0012)

    Args:
        conn: The request's connection, inside the caller's transaction.
        idempotency_key: The client's Idempotency-Key.
        request_hash: The canonical hash of the request.
        reverses_transaction_id: The transaction this one reverses, if any.

    Returns:
        The transaction id, and whether it is a replay.

    Raises:
        IdempotencyConflictError: The key was already used with a different request.
        LedgerInvariantError: The key conflicted but its row cannot be found.
            Also, propagates repository level safety-net violations.
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
        else:
            raise IdempotencyConflictError(
                "Idempotency key was already used with a different request"
            )
    return Claim(transaction_id=transaction_id, replay=False)


@transactional_service_method
def post_transaction(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    """Validate and write the entries of a claimed transaction. (rules 1, 4, 6)

    The checks run in a fixed order and nothing is written until all of them
    pass, so a rejected post leaves no entries and no balance change.

    Checks:
    1. entries are present
    2. they sum to zero per currency (rule 1)
    3. the entries are netted into one delta per account
    4. the accounts are locked in ascending id order (ADR 0006) and each must exist
    5. each entry's currency matches its account (rule 6)
    6. no account with allow_negative false would go below zero (rule 4).
    7. The overdraft check uses the balance returned by the same statement that took
       the lock, so no other transaction can change it before this one commits.

    Only then are the entries inserted and the balances updated.

    The caller owns the transaction. This function never commits, and on an
    error the caller's rollback also removes the claim. (ADR 0005)

    Args:
        conn: The request's connection, inside the caller's transaction.
        transaction_id: The id returned by claim_transaction.
        entries: The entries to write; negative amounts debit, positive credit.

    Raises:
        LedgerInvariantError: There are no entries, or a balance row was not updated.
            Also, propagates repository level safety-net violations.
        UnbalancedEntriesError: The entries do not sum to zero in some currency.
        UnknownAccountError: An entry names an account that does not exist.
        CurrencyMismatchError: An entry's currency differs from its account's.
        InsufficientFundsError: An account that may not go negative would.
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
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    from_account_id: int,
    to_account_id: int,
    currency: str,
    amount_minor: int,
) -> Transfer:
    """Claim the key and post a two-entry transfer in the caller's transaction. (rule 5)

    On a replay the post is skipped, so the money moves once, and the stored
    transfer is returned. The claim and the posting share the one connection
    and so one database transaction: if the post fails, rolling back removes
    the claim too and the key is not used up. (ADR 0005)

    Args:
        conn: The request's connection, inside the caller's transaction.
        idempotency_key: The client's Idempotency-Key.
        request_hash: The canonical hash of the request.
        from_account_id: The account debited.
        to_account_id: The account credited.
        currency: The currency of the transfer.
        amount_minor: The amount, in minor units.

    Returns:
        The transfer, newly posted or replayed, with its entries.

    Raises:
        IdempotencyConflictError: The key was already used with a different request.
        UnknownAccountError: The debit or credit account does not exist.
        CurrencyMismatchError: The transfer currency differs from an account's currency.
        InsufficientFundsError: The debit account cannot cover the transfer amount.
        LedgerInvariantError: A critical state check failed or a balance row was not updated.
    """
    claim = claim_transaction(conn, idempotency_key, request_hash)
    if not claim.replay:
        post_transaction(
            conn,
            claim.transaction_id,
            entries_for_transfer(from_account_id, to_account_id, amount_minor, currency),
        )
    return get_transfer(conn, claim.transaction_id)
