import psycopg
from psycopg import errors

from ledger.errors import LedgerInvariantError
from ledger.models.posting_model import Entry, LockedAccount, Transfer, TransferEntry

INSERT_CLAIM_SQL = """
                     INSERT INTO 
                         transactions (idempotency_key, request_hash, reverses_transaction_id) 
                     VALUES (%s, %s, %s) 
                     ON CONFLICT (idempotency_key) DO NOTHING 
                     RETURNING id;
                     """


SELECT_CLAIM_SQL = """
                        SELECT 
                            id, 
                            request_hash 
                        FROM transactions
                        WHERE idempotency_key = %s;
                        """


INSERT_ENTRY_SQL = """
                   INSERT INTO 
                       entries (transaction_id, account_id, amount_minor, currency) 
                   VALUES (%s, %s, %s, %s);
                   """

UPDATE_BALANCE_SQL = """UPDATE 
                            balances 
                        SET balance_minor = balance_minor + %s 
                        WHERE account_id = %s
                        RETURNING account_id;
                     """


LOCK_ACCOUNTS_SQL = """
                    SELECT 
                        b.account_id, 
                        a.currency, 
                        b.allow_negative, 
                        b.balance_minor 
                    FROM balances b
                    JOIN accounts a 
                    ON a.id = b.account_id
                    WHERE b.account_id = ANY(%s::bigint[])
                    ORDER BY b.account_id ASC
                    FOR UPDATE OF b;
                    """


SELECT_TRANSFER_SQL = """
                      SELECT 
                          id, 
                          reverses_transaction_id, 
                          created_at 
                      FROM transactions 
                      WHERE id = %s;
                      """


SELECT_ENTRIES_SQL = """
                     SELECT
                        transaction_id,
                         account_id, 
                         amount_minor, 
                         currency 
                     FROM entries 
                     WHERE transaction_id = %s;
                     """


def insert_claim(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> int | None:
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                INSERT_CLAIM_SQL, (idempotency_key, request_hash, reverses_transaction_id)
            )
            row = cursor.fetchone()
            if row:
                return row[0]
            return None
    except errors.IntegrityError as failure:
        raise LedgerInvariantError("Critical error on insertion of transfer record") from failure


def find_claim(conn: psycopg.Connection, idempotency_key: str) -> tuple[int, str] | None:
    with conn.cursor() as cursor:
        cursor.execute(SELECT_CLAIM_SQL, (idempotency_key,))
        row = cursor.fetchone()
        return row


def lock_accounts(conn: psycopg.Connection, account_ids: list[int]) -> dict[int, LockedAccount]:
    with conn.cursor() as cursor:
        cursor.execute(LOCK_ACCOUNTS_SQL, (account_ids,))
        rows = cursor.fetchall()
        return {row[0]: LockedAccount(row[1], row[2], row[3]) for row in rows}


def insert_entries(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    try:
        with conn.cursor() as cursor:
            params = [
                (transaction_id, entry.account_id, entry.amount_minor, entry.currency)
                for entry in entries
            ]
            cursor.executemany(INSERT_ENTRY_SQL, params)
    except errors.IntegrityError as failure:
        raise LedgerInvariantError("Critical error on insertion of entry record") from failure


def apply_balance_deltas(conn: psycopg.Connection, deltas: dict[int, int]) -> set[int]:
    try:
        with conn.cursor() as cursor:
            updated_accounts = set()
            for key, delta in deltas.items():
                cursor.execute(UPDATE_BALANCE_SQL, (delta, key))
                row = cursor.fetchone()
                if row:
                    updated_accounts.add(row[0])
            return updated_accounts
    except errors.IntegrityError as failure:
        raise LedgerInvariantError("Critical error on balance update") from failure


def find_transfer(conn: psycopg.Connection, id: int) -> Transfer | None:
    with conn.cursor() as cursor:
        cursor.execute(SELECT_TRANSFER_SQL, (id,))
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(SELECT_ENTRIES_SQL, (id,))
        entries: list[TransferEntry] = []
        for _i in range(0, cursor.rowcount):
            row = cursor.fetchone()
            if row is not None:
                entries.append(TransferEntry(*row))
        return Transfer(
            id=row[0], reverses_transaction_id=row[1], created_at=row[2], entries=tuple(entries)
        )
