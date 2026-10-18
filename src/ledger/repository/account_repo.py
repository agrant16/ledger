import psycopg
from psycopg import errors

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError
from ledger.models.account_model import Account, Balance
from ledger.models.entry_history_model import HistoryEntry
from ledger.models.transfer_model import TransferEntry

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
    with conn.cursor() as cursor:
        try:
            cursor.execute(INSERT_ACCOUNTS_SQL, (account_name, currency, allow_negative))
            row = cursor.fetchone()
            account = Account(
                id=row[0], account_name=row[1], currency=row[2], allow_negative=row[3]
            )
            return account
        except errors.UniqueViolation as failure:
            constraint = failure.diag.constraint_name
            if constraint == "accounts_account_name_key":
                raise DuplicateAccountNameError(account_name) from failure
            raise LedgerInvariantError("Critical error in insert account") from failure
        except errors.IntegrityError as failure:
            raise LedgerInvariantError("Critical error on in insert account") from failure


def insert_balance(conn: psycopg.Connection, account_id: int, allow_negative: bool) -> None:
    with conn.cursor() as cursor:
        try:
            cursor.execute(INSERT_BALANCES_SQL, (account_id, allow_negative))
        except errors.IntegrityError as failure:
            raise LedgerInvariantError(
                f"Critical Error encountered inserting balance record for account {account_id}"
            ) from failure


def find_balance(conn: psycopg.Connection, account_id: int) -> Balance | None:
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
    with conn.cursor() as cursor:
        cursor.execute(ACCOUNT_EXISTS_SQL, (account_id,))
        row = cursor.fetchone()
        return row[0]


def find_entries(conn: psycopg.Connection, account_id: int, before_id: int | None, limit: int) -> tuple[HistoryEntry, ...]:
    with conn.cursor() as cursor:
        cursor.execute(FIND_ENTRIES_SQL, (account_id, before_id, limit))
        rows = cursor.fetchall()
        entries = tuple([HistoryEntry(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows])
        return entries
