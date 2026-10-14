import psycopg
from psycopg import errors

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError
from ledger.models.account_model import Account, Balance

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
