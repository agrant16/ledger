from dataclasses import dataclass

import psycopg
from psycopg import errors

from ledger.errors import DuplicateAccountNameError, LedgerInvariantError

insert_accounts_sql = """ 
                      INSERT INTO 
                          accounts (name, currency, allow_negative) 
                          VALUES(%s, %s, %s) 
                          RETURNING id, name, currency, allow_negative;
                      """

insert_balances_sql = """
                      INSERT INTO 
                         balances (account_id, allow_negative, balance_minor) 
                         VALUES (%s, %s, 0);
                      """


@dataclass(frozen=True)
class Account:
    account_id: int
    name: str
    currency: str
    allow_negative: bool


def create_account(
    conn: psycopg.Connection, name: str, currency: str, allow_negative: bool
) -> Account:
    if conn.autocommit:
        raise LedgerInvariantError("create_account needs a connection with a transaction in progress")

    with conn.cursor() as cursor:
        try:
            cursor.execute(insert_accounts_sql, (name, currency, allow_negative))
            record = cursor.fetchone()
            account = Account(**record)
            cursor.execute(insert_balances_sql, (account.account_id, account.allow_negative))
            return account
        except errors.UniqueViolation as e:
            constraint = e.diag.constraint_name
            if constraint == 'account_name_key':
                raise DuplicateAccountNameError(name) from e
            raise e
