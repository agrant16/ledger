from dataclasses import dataclass

import psycopg
from psycopg import errors

from ledger.db import require_transaction
from ledger.errors import DuplicateAccountNameError, UnknownAccountError

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
                      b.account_id, 
                      a.currency,
                      b.balance_minor 
                  FROM accounts a 
                  LEFT JOIN balances b 
                  ON a.id = b.account_id
                  WHERE a.id = %s;
                  """


@dataclass(frozen=True)
class Account:
    id: int
    account_name: str
    currency: str
    allow_negative: bool


@dataclass(frozen=True)
class Balance:
    account_id: int
    currency: str
    balance_minor: int


def create_account(
    conn: psycopg.Connection, account_name: str, currency: str, allow_negative: bool
) -> Account:
    require_transaction(conn, "create_account")

    with conn.cursor() as cursor:
        try:
            cursor.execute(INSERT_ACCOUNTS_SQL, (account_name, currency, allow_negative))
            account = Account(*cursor.fetchone())
            cursor.execute(INSERT_BALANCES_SQL, (account.id, account.allow_negative))
            return account
        except errors.UniqueViolation as e:
            constraint = e.diag.constraint_name
            if constraint == "accounts_account_name_key":
                raise DuplicateAccountNameError(account_name) from e
            raise


def get_balance(conn: psycopg.Connection, account_id: int) -> Balance:
    with conn.cursor() as cursor:
        cursor.execute(GET_BALANCE_SQL, (account_id,))
        row = cursor.fetchone()
        if row is None:
            raise UnknownAccountError(
                f"Unknown account {account_id} encountered while getting balance"
            )
        return Balance(account_id=row[0], currency=row[1], balance_minor=row[2])
