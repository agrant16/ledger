import psycopg

from ledger.errors import LedgerInvariantError, UnknownAccountError
from ledger.models.account_model import Account, Balance
from ledger.models.entry_history_model import MAX_PAGE_SIZE, EntryPage
from ledger.repository import account_repo
from ledger.service.decorators import transactional_service_method


@transactional_service_method
def create_account(
    conn: psycopg.Connection, account_name: str, currency: str, allow_negative: bool
) -> Account:
    """Creates an account and its zero balance row in the caller's transaction.

    Args:
        conn: The request's connection, inside the caller's transaction.
        account_name: The account's unique name.
        currency: The account's currency code.
        allow_negative: Whether the account may go below zero.

    Returns:
        The created account.

    Raises:
        DuplicateAccountNameError: The account name is already taken.
        LedgerInvariantError: The account exists but has no balance row a repository-level
            data integrity constraint is violated.
    """
    account = account_repo.insert_account(conn, account_name, currency, allow_negative)
    account_repo.insert_balance(conn, account.id, account.allow_negative)
    return account


@transactional_service_method
def get_balance(conn: psycopg.Connection, account_id: int) -> Balance:
    """Returns the cached balance of an account. (rule 3)

    Args:
        conn: The request's connection, inside the caller's transaction.
        account_id: The account whose balance is read.

    Returns:
        The cached balance, with `balance_minor` set.

    Raises:
        UnknownAccountError: The account does not exist.
        LedgerInvariantError: The account exists but has no balance row.
    """
    balance = account_repo.find_balance(conn, account_id)
    if balance is None:
        raise UnknownAccountError(f"Unknown account {account_id} encountered while getting balance")

    if balance.balance_minor is None:
        raise LedgerInvariantError(f"Critical error: Account {account_id} has no balance row")

    return balance


@transactional_service_method
def find_account_by_name(conn: psycopg.Connection, account_name: str) -> Account | None:
    """Finds an account by its name.

    Args:
        conn: The request's connection, inside the caller's transaction.
        account_name: The exact, case-sensitive account name.

    Returns:
        The account, or None if no account has this name.
    """
    return account_repo.find_account_by_name(conn, account_name)


@transactional_service_method
def get_entry_history(
    conn: psycopg.Connection, account_id: int, before_id: int | None, limit: int
) -> EntryPage:
    """Returns one page of an account's entries, newest first. (ADR 0014)

    The page is the entries with id below `before_id` (or the newest, if
    `before_id` is None), ordered by id descending.

    The API guarantees 1 <= `limit` <= `MAX_PAGE_SIZE`, so a value outside that
    range is a bug and not a client error.

    Args:
        conn: The request's connection, inside the caller's transaction.
        account_id: The account whose entries are listed.
        before_id: The id of the last entry already seen, or None for the first page.
        limit: The page size.

    Returns:
        An `EntryPage` of at most `limit` entries, with `has_more` set when older ones exist.

    Raises:
        LedgerInvariantError: The limit is outside the allowed range.
        UnknownAccountError: The account does not exist.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise LedgerInvariantError(f"Critical error: page size {limit} not within range")
    # The account check is a separate statement from the entries query.
    # That is race-free because accounts are never deleted, so an account
    # that exists now still exists when the entries are read.
    if not account_repo.account_exists(conn, account_id):
        raise UnknownAccountError(
            f"Unknown account {account_id} encountered while getting entry history"
        )
    entries = account_repo.find_entries(conn, account_id, before_id, limit + 1)
    has_more = len(entries) > limit
    return EntryPage(tuple(entries[:limit]), has_more)
