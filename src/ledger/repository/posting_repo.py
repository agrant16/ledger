from datetime import UTC

import psycopg
from psycopg import errors

from ledger.errors import LedgerInvariantError
from ledger.models.posting_model import Entry, LockedAccount
from ledger.models.transfer_model import Transfer, TransferEntry

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
                         id,
                         account_id, 
                         amount_minor, 
                         currency 
                     FROM entries 
                     WHERE transaction_id = %s
                     ORDER BY id;
                     """


def insert_claim(
    conn: psycopg.Connection,
    idempotency_key: str,
    request_hash: str,
    reverses_transaction_id: int | None = None,
) -> int | None:
    """Inserts the transactions row that claims an idempotency key. (rule 5)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        idempotency_key: The client's Idempotency-Key.
        request_hash: The canonical hash of the request.
        reverses_transaction_id: The transaction this one reverses, if any.

    Returns:
        The new transaction id, or None if the key already existed, in which
        case nothing is written.

    Raises:
        LedgerInvariantError: The database safety net rejected the insert.
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                INSERT_CLAIM_SQL, (idempotency_key, request_hash, reverses_transaction_id)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except errors.IntegrityError as failure:
        raise LedgerInvariantError("Critical error on insertion of transfer record") from failure


def find_claim(conn: psycopg.Connection, idempotency_key: str) -> tuple[int, str] | None:
    """Finds the claim for an idempotency key.

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        idempotency_key: The client's Idempotency-Key.

    Returns:
        `(transaction_id, request_hash)`, or None if the key has not been used.
    """
    with conn.cursor() as cursor:
        return cursor.execute(SELECT_CLAIM_SQL, (idempotency_key,)).fetchone()


def lock_accounts(conn: psycopg.Connection, account_ids: list[int]) -> dict[int, LockedAccount]:
    """Locks the balance rows of the given accounts and reads them. (rule 4)

    The rows stay locked until the caller's transaction ends. The locks are taken
    in ascending account id, which keeps concurrent transfers from deadlocking.
    (ADR 0006)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        account_ids: The accounts to lock.

    Returns:
        The locked accounts keyed by account id, in ascending id order. Each holds
        the account's currency, its allow_negative flag and its balance. An unknown
        account id is absent. Overdraft must be checked against these balances,
        not a separate read.
    """
    with conn.cursor() as cursor:
        cursor.execute(LOCK_ACCOUNTS_SQL, (account_ids,))
        rows = cursor.fetchall()
        return {row[0]: LockedAccount(row[1], row[2], row[3]) for row in rows}


def insert_entries(conn: psycopg.Connection, transaction_id: int, entries: list[Entry]) -> None:
    """Inserts the entries of a transaction. (rules 1, 6)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        transaction_id: The id of the transaction associated with the entries.
        entries: The entries to be inserted.

    Raises:
        LedgerInvariantError: The database safety net rejected an entry.
    """
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
    """Adds each delta to its account's cached balance. (rule 3)

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        deltas: The balance deltas in the form {"account_id": net_change_delta}.

    Returns:
        The ids of the accounts whose balance row was updated. A requested id
        missing from the result has no balance row.

    Raises:
        LedgerInvariantError: The database safety net rejected an update.
    """
    try:
        with conn.cursor() as cursor:
            updated_accounts = set()
            for account_id, delta in deltas.items():
                cursor.execute(UPDATE_BALANCE_SQL, (delta, account_id))
                row = cursor.fetchone()
                if row:
                    updated_accounts.add(row[0])
            return updated_accounts
    except errors.IntegrityError as failure:
        raise LedgerInvariantError("Critical error on balance update") from failure


def find_transfer(conn: psycopg.Connection, id: int) -> Transfer | None:
    """Finds a transfer and its entries.

    Args:
        conn: An active, open PostgreSQL connection or transaction context.
        id: The transaction id.

    Returns:
        The transfer, with its entries ordered by entry id, or None if there is no such
        transfer.
    """
    with conn.cursor() as cursor:
        cursor.execute(SELECT_TRANSFER_SQL, (id,))
        transfer_row = cursor.fetchone()
        if transfer_row is None:
            return None
        cursor.execute(SELECT_ENTRIES_SQL, (id,))
        entries_rows = cursor.fetchall()
        entries: list[TransferEntry] = [TransferEntry(*row) for row in entries_rows]
        return Transfer(
            id=transfer_row[0],
            reverses_transaction_id=transfer_row[1],
            created_at=transfer_row[2].astimezone(UTC),
            entries=tuple(entries),
        )
