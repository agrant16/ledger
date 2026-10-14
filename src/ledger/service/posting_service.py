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
from ledger.repository import posting_repo
from ledger.service.decorators import transactional_service_method


@transactional_service_method
def claim_transaction(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> Claim:
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
    if not entries:
        raise LedgerInvariantError(f"No entries for transaction {transaction_id}")

    # check that entries for each account and currency sum to 0
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
