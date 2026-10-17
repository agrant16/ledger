"""The ledger invariants as one check that any test with a database connection can run.

Used by the state-machine tests after every step (tests/property), and meant for the concurrency
tests of milestone 4 after each run. It reads through the connection it is given, so pass the
owner connection: it sees what has been committed and needs no particular privilege.

The numbers are those of the invariants list in docs/design.md. This helper checks 1 (every
transaction sums to zero per currency), 3 (every cached balance equals the sum of its entries),
4 (no protected account is negative) and 6 (every entry's currency matches its account's), plus
one consequence of rule 1 that is worth failing on its own: the per-currency total across all
accounts is zero. Rule 2 (append-only) is a privilege rule, tested in tests/integration/db, and
rule 5 (a key never moves money twice) needs requests, so each test of it makes its own check.
"""

import psycopg


def assert_ledger_consistent(conn: psycopg.Connection) -> None:
    def count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return row[0]

    assert not count(
        "SELECT count(*) FROM (SELECT 1 FROM entries GROUP BY transaction_id, currency"
        " HAVING sum(amount_minor) <> 0) t"
    ), "a transaction does not sum to zero in some currency"

    assert not count(
        "SELECT count(*) FROM (SELECT 1 FROM entries GROUP BY currency"
        " HAVING sum(amount_minor) <> 0) t"
    ), "a currency does not sum to zero across all accounts"

    assert not count(
        "SELECT count(*) FROM balances b"
        " LEFT JOIN (SELECT account_id, sum(amount_minor) AS total FROM entries"
        "            GROUP BY account_id) e ON e.account_id = b.account_id"
        " WHERE b.balance_minor <> COALESCE(e.total, 0)"
    ), "a cached balance differs from the sum of its entries"

    assert not count(
        "SELECT count(*) FROM balances WHERE NOT allow_negative AND balance_minor < 0"
    ), "a protected account is negative"

    assert not count(
        "SELECT count(*) FROM entries e JOIN accounts a ON a.id = e.account_id"
        " WHERE e.currency <> a.currency"
    ), "an entry's currency differs from its account's"
