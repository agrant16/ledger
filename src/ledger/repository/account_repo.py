from datetime import UTC

import psycopg
from psycopg import errors

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError
from ledger.models.account_model import Account, Balance
from ledger.models.entry_history_model import HistoryEntry

INSERT_ACCOUNTS_SQL = """ 
                      INSERT INTO 
                          accounts (account_name, currency, allow_negative) 
                          VALUES(%s, %s, %s) 
                          RETURNING id, account_name, currency, allow_negative;
                      """

INSERT_BALANCES_SQL = """
                      INSERT INTO 
                         balances (account_id, allow_negative, balance_minor) 
                         VALUES (%s, %s, 0);
                      """

GET_BALANCE_SQL = """
                  SELECT 
                      a.id, 
                      a.currency,
                      b.balance_minor 
                  FROM accounts a 
                  LEFT JOIN balances b 
                  ON a.id = b.account_id
                  WHERE a.id = %s;
                  """

GET_ACCOUNT_BY_NAME_SQL = """
                          SELECT 
                              id, 
                              account_name, 
                              currency, 
                              allow_negative
                          FROM accounts 
                          WHERE account_name = %s;
                          """


ACCOUNT_EXISTS_SQL = """
                     SELECT EXISTS (
                         SELECT 
                             1 
                         FROM accounts 
                         WHERE id = %s
                     );
                     """

FIND_ENTRIES_SQL = """
                   SELECT 
                       e.id, 
                       e.transaction_id, 
                       e.account_id, 
                       e.amount_minor, 
                       e.currency, 
                       t.created_at 
                   FROM entries e JOIN transactions t ON t.id = e.transaction_id 
                   WHERE e.account_id = %s AND e.id < COALESCE(%s, 9223372036854775807)
                   ORDER BY e.id DESC
                   LIMIT %s;
                   """


def insert_account(
    conn: psycopg.Connection, account_name: str, currency: str, allow_negative: bool
) -> Account:
    """Insert an account row. (ADR 0013)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_name: The account's unique name.
        currency: The account's currency code.
        allow_negative: Whether the account may go below zero.

    Returns:
        The stored account.

    Raises:
        DuplicateAccountNameError: An account with this name already exists.
        LedgerInvariantError: The database safety net rejected the insert for any
            other reason.
    """
    with conn.cursor() as cursor:
        try:
            cursor.execute(INSERT_ACCOUNTS_SQL, (account_name, currency, allow_negative))
        except errors.UniqueViolation as failure:
            constraint = failure.diag.constraint_name
            if constraint == "accounts_account_name_key":
                raise DuplicateAccountNameError(account_name) from failure
            raise LedgerInvariantError("Critical error in insert account") from failure
        except errors.IntegrityError as failure:
            raise LedgerInvariantError("Critical error in insert account") from failure
        row = cursor.fetchone()
        return Account(*row)


def insert_balance(conn: psycopg.Connection, account_id: int, allow_negative: bool) -> None:
    """Insert an account's zero balance row. (rule 3)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_id: The account's unique ID.
        allow_negative: Whether the account may go below zero (protected account or not).

    Raises:
        LedgerInvariantError: The database safety net rejected the insert.
    """
    with conn.cursor() as cursor:
        try:
            cursor.execute(INSERT_BALANCES_SQL, (account_id, allow_negative))
        except errors.IntegrityError as failure:
            raise LedgerInvariantError(
                f"Critical Error encountered inserting balance record for account {account_id}"
            ) from failure


def find_balance(conn: psycopg.Connection, account_id: int) -> Balance | None:
    """Return the cached balance of an account, or None if the account does not exist.

    Its balance_minor is None if the account has no balance row.

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_id: The account's unique ID.

    Returns:
        A fully populated ``Balance`` object. Returns ``None`` if no account matches the
        provided ID.
    """
    with conn.cursor() as cursor:
        cursor.execute(GET_BALANCE_SQL, (account_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return Balance(account_id=row[0], currency=row[1], balance_minor=row[2])


def find_account_by_name(conn: psycopg.Connection, account_name: str) -> Account | None:
    with conn.cursor() as cursor:
        cursor.execute(GET_ACCOUNT_BY_NAME_SQL, (account_name,))
        row = cursor.fetchone()
        if row is None:
            return None
        return Account(id=row[0], account_name=row[1], currency=row[2], allow_negative=row[3])


def account_exists(conn: psycopg.Connection, account_id: int) -> bool:
    """Checks the database to verify if an account ID exists.

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_id: The account identifier to check.

    Returns:
        True if the account is registered in the ledger, False otherwise.
    """
    with conn.cursor() as cursor:
        return cursor.execute(ACCOUNT_EXISTS_SQL, (account_id,)).fetchone()[0]


def find_entries(
    conn: psycopg.Connection, account_id: int, before_id: int | None, limit: int
) -> list[HistoryEntry]:
    """Return up to limit entries of an account, newest first.

    Only entries with an id below before_id are returned. A before_id of None
    starts from the newest entry. (ADR 0014)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_id: The account's unique ID.
        before_id: An integer representing the where to cut off the returned entries.
        limit: The maximum number of entries to return.

    Returns:
        A list of fully populated ``HistoryEntry`` objects.
    """
    with conn.cursor() as cursor:
        cursor.execute(FIND_ENTRIES_SQL, (account_id, before_id, limit))
        rows = cursor.fetchall()
        return [
            HistoryEntry(
                id=row[0],
                transaction_id=row[1],
                account_id=row[2],
                amount_minor=row[3],
                currency=row[4],
                created_at=row[5].astimezone(UTC),
            )
            for row in rows
        ]
