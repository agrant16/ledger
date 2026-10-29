"""A deliberately wrong balances locker, for the negative control of the opposing-transfers test.

The real `posting_repo.lock_accounts` takes every touched `balances` row in one statement,
ordered by ascending account id (ADR 0006). This one takes them in the order the request names
them, one statement per account, and waits at a barrier after the first lock. Two opposing
transfers that both reach the barrier each hold one row and then ask for the other's, so they
deadlock every time. It lives in the tests and is never imported from `src`.
"""

import threading
from collections.abc import Callable

import psycopg

from ledger.models.posting_model import LockedAccount

LOCK_ONE_ACCOUNT_SQL = """
    SELECT b.account_id, a.currency, b.allow_negative, b.balance_minor
    FROM balances b
    JOIN accounts a ON a.id = b.account_id
    WHERE b.account_id = %s
    FOR UPDATE OF b;
"""

# One statement like `posting_repo.LOCK_ACCOUNTS_SQL`, but ordered by the position of each id in the
# request and not by id, so the rows are locked in request order.
LOCK_IN_REQUEST_ORDER_SQL = """
    SELECT b.account_id, a.currency, b.allow_negative, b.balance_minor
    FROM unnest(%s::bigint[]) WITH ORDINALITY AS r (account_id, position)
    JOIN balances b ON b.account_id = r.account_id
    JOIN accounts a ON a.id = b.account_id
    ORDER BY r.position
    FOR UPDATE OF b;
"""

LockAccounts = Callable[[psycopg.Connection, list[int]], dict[int, LockedAccount]]


def lock_in_request_order(after_first_lock: threading.Barrier) -> LockAccounts:
    """Builds a drop-in for `posting_repo.lock_accounts` that locks in request order.

    Args:
        after_first_lock: The barrier every caller waits at once it holds its first row. Its
            party count must equal the number of transfers fired together.

    Returns:
        A function with the signature and result of `posting_repo.lock_accounts`.
    """

    def lock_accounts(conn: psycopg.Connection, account_ids: list[int]) -> dict[int, LockedAccount]:
        locked: dict[int, LockedAccount] = {}
        for position, account_id in enumerate(account_ids):
            row = conn.execute(LOCK_ONE_ACCOUNT_SQL, (account_id,)).fetchone()
            if row is not None:
                locked[row[0]] = LockedAccount(row[1], row[2], row[3])
            if position == 0:
                after_first_lock.wait()
        return locked

    return lock_accounts
