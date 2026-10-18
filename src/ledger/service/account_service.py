import psycopg

from ledger.errors import LedgerInvariantError, UnknownAccountError
from ledger.models.account_model import Account, Balance
from ledger.models.entry_history_model import EntryPage
from ledger.repository import account_repo
from ledger.service.decorators import transactional_service_method


@transactional_service_method
def create_account(
    conn: psycopg.Connection, account_name: str, currency: str, allow_negative: bool
) -> Account:
    account = account_repo.insert_account(conn, account_name, currency, allow_negative)
    account_repo.insert_balance(conn, account.id, account.allow_negative)
    return account


@transactional_service_method
def get_balance(conn: psycopg.Connection, account_id: int) -> Balance:
    balance = account_repo.find_balance(conn, account_id)
    if balance is None:
        raise UnknownAccountError(f"Unknown account {account_id} encountered while getting balance")

    if balance.balance_minor is None:
        raise LedgerInvariantError(f"Critical error: Account {account_id} has no balance row")

    return balance


@transactional_service_method
def find_account_by_name(conn: psycopg.Connection, account_name: str) -> Account | None:
    return account_repo.find_account_by_name(conn, account_name)


@transactional_service_method
def get_entry_history(
    conn: psycopg.Connection, account_id: int, before_id: int | None, limit: int
) -> EntryPage:
    if limit < 1 or limit > 200:
        raise LedgerInvariantError(f"Critical error: page size {limit} not within range")
    if not account_repo.account_exists(conn, account_id):
        raise UnknownAccountError(
            f"Unknown account {account_id} encountered while getting entry history"
        )
    entries = account_repo.find_entries(conn, account_id, before_id, limit + 1)
    has_more = False
    if len(entries) > limit:
        has_more = True
        entries = entries[:limit]
    return EntryPage(tuple(entries), has_more)
