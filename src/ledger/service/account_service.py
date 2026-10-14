import psycopg

from ledger.errors import LedgerInvariantError, UnknownAccountError
from ledger.models.account_model import Account, Balance
from ledger.repository import account_repo
from ledger.service.decorators import transactional_service_method


@transactional_service_method
def create_account(
    conn: psycopg.Connection, account_name: str, currency: str, allow_negative: bool = False
) -> Account:
    account = account_repo.insert_account(conn, account_name, currency, allow_negative)
    account_repo.insert_balance(conn, account.id, allow_negative)
    return account


@transactional_service_method
def get_balance(conn: psycopg.Connection, account_id: int) -> Balance:
    balance = account_repo.find_balance(conn, account_id)
    if balance is None:
        raise UnknownAccountError(f"Unknown account {account_id} encountered while getting balance")

    if balance.balance_minor is None:
        raise LedgerInvariantError(f"Critical error: Account {account_id} has no balance row")

    return balance
