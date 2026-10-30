"""A deliberately wrong balance reader, for the negative control of the double-spend test.

The real `posting_repo.lock_accounts` returns the balance from the same statement that takes the
row lock, and the overdraft check uses that balance (rule 4, ADR 0006). This one reads the balances
with a plain SELECT first, waits at a barrier so every caller has read before any has locked, then
takes the real lock but hands back the balances from the plain SELECT. Two transfers that together
exceed a balance both pass the check. It lives in the tests and is never imported from `src`.
"""

import threading

import psycopg

from ledger.models.posting_model import LockedAccount
from ledger.repository import posting_repo
from tests.concurrency.bad_locking import LockAccounts

READ_BALANCES_SQL = """
    SELECT b.account_id, b.balance_minor
    FROM balances b
    WHERE b.account_id = ANY(%s::bigint[]);
"""


def read_before_locking(after_read: threading.Barrier) -> LockAccounts:
    """Builds a drop-in for `posting_repo.lock_accounts` that checks against a stale balance.

    Call this before patching `posting_repo.lock_accounts`, so the real function is the one wrapped.

    Args:
        after_read: The barrier every caller waits at once it has read the balances. Its party
            count must equal the number of transfers fired together.

    Returns:
        A function with the signature of `posting_repo.lock_accounts` whose balances are the ones
        read before the lock.
    """
    real_lock_accounts = posting_repo.lock_accounts

    def lock_accounts(conn: psycopg.Connection, account_ids: list[int]) -> dict[int, LockedAccount]:
        stale = dict(conn.execute(READ_BALANCES_SQL, (account_ids,)).fetchall())
        after_read.wait()
        locked = real_lock_accounts(conn, account_ids)
        return {
            account_id: LockedAccount(row.currency, row.allow_negative, stale[account_id])
            for account_id, row in locked.items()
        }

    return lock_accounts
