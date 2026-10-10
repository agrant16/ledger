import psycopg

from dataclasses import dataclass
from ledger.errors import DuplicateAccountNameError, InvalidRequestError
from psycopg import errors

insert_accounts_sql = """ 
                      INSERT INTO 
                          accounts (id, name, currency, allow_negative) 
                          VALUES(%s, %s, %s, %s) 
                          RETURNING id, name, currency;
                      """

insert_balances_sql = """
                      INSERT INTO 
                         balances (account_id, allow_negative, balance_mior) 
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
            cursor.execute(insert_balances_sql, (id, allow_negative))
            record = cursor.fetchone()
            account_id, name, currency, allow_negative = record
            return Account(
                id=account_id, name=name, currency=currency, allow_negative=allow_negative
            )
        except errors.UniqueViolation as e:
            constraint = e.diag.constraint_name
            if constraint == "accounts_name_key":
                raise DuplicateAccountNameError(name)
