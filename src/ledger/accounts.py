import psycopg

from dataclasses import dataclass
from ledger.errors import LedgerInvariantError, DuplicateAccountNameError
from psycopg import errors

insert_accounts_sql = """ 
                      INSERT INTO 
                          accounts (name, currency, allow_negative) 
                          VALUES(%s, %s, %s, %s) 
                          RETURNING id, name, currency, allow_negative;
                      """

insert_balances_sql = """
                      INSERT INTO 
                         balances (account_id, allow_negative, balance_minor) 
                         VALUES (%s, %s, 0);
                      """


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    currency: str
    allow_negative: bool


def create_account(
    conn: psycopg.Connection, name: str, currency: str, allow_negative: bool
) -> Account | None:
    if conn.autocommit:
        raise LedgerInvariantError()

    with conn.cursor() as cursor:
        try:
            cursor.execute(insert_accounts_sql, (id, name, currency, allow_negative))
            record = cursor.fetchone()
            account_id, name, currency, allow_negative = record
            cursor.execute(insert_balances_sql, (id, allow_negative))
            return Account(
                id=account_id, name=name, currency=currency, allow_negative=allow_negative
            )
        except errors.UniqueViolation as e:
            raise DuplicateAccountNameError(name)
