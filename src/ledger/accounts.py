from dataclasses import dataclass

import psycopg
from psycopg import errors

from ledger.db import require_transaction
from ledger.errors import DuplicateAccountNameError

INSERT_ACCOUNTS_SQL = """ 
                      INSERT INTO 
                          accounts (account_name, currency, allow_negative) 
                          VALUES(%s, %s, %s) 
                          RETURNING id, name, currency, allow_negative;
                      """

INSERT_BALANCES_SQL = """
                      INSERT INTO 
                         balances (account_id, allow_negative, balance_minor) 
                         VALUES (%s, %s, 0);
                      """


@dataclass(frozen=True)
class Account:
    id: int
    account_name: str
    currency: str
    allow_negative: bool


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
