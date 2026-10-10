"""Properties of the balances table (docs/design.md, Tables) checked through the app role.

balances is a cache of the entries. Its constraints are the safety net behind the transfer code:
a protected account (allow_negative false) can never hold a negative balance, and a row's
allow_negative can never disagree with its account's.
"""

import re
from pathlib import Path

import psycopg
import pytest

from ledger.config import Settings
from ledger.migrate import run_migrations

BIGINT_MAX = 9_223_372_036_854_775_807

INSERT_BALANCE = (
    "INSERT INTO balances (account_id, allow_negative, balance_minor) VALUES (%s, %s, %s)"
)


def insert_account(
    conn: psycopg.Connection,
    name: str = "customer:alice",
    currency: str = "USD",
    allow_negative: bool = False,
) -> int:
    row = conn.execute(
        "INSERT INTO accounts (name, currency, allow_negative) VALUES (%s, %s, %s) RETURNING id",
        (name, currency, allow_negative),
    ).fetchone()
    assert row is not None
    return row[0]


def balance_of(conn: psycopg.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_balance_is_a_not_null_bigint(owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'balances' AND column_name = 'balance_minor'"
    ).fetchone()
    assert row == ("bigint", "NO")


@pytest.mark.parametrize("allow_negative", [False, True])
def test_balance_row_can_be_created_for_an_existing_account(
    clean_db: None, app_conn: psycopg.Connection, allow_negative: bool
) -> None:
    account = insert_account(app_conn, allow_negative=allow_negative)
    app_conn.execute(INSERT_BALANCE, (account, allow_negative, 0))
    assert balance_of(app_conn, account) == 0


def test_account_id_is_the_primary_key(clean_db: None, app_conn: psycopg.Connection) -> None:
    """One balances row per account: this is the row every writer locks."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(INSERT_BALANCE, (account, False, 0))


def test_balance_row_must_reference_an_existing_account(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(INSERT_BALANCE, (999_999, False, 0))


@pytest.mark.parametrize("account_allows_negative", [False, True])
def test_balance_allow_negative_must_match_the_accounts(
    clean_db: None, app_conn: psycopg.Connection, account_allows_negative: bool
) -> None:
    """The copy of allow_negative cannot drift from the account's, in either direction."""
    account = insert_account(app_conn, allow_negative=account_allows_negative)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(INSERT_BALANCE, (account, not account_allows_negative, 0))


def test_allow_negative_cannot_be_changed_on_an_existing_row(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """The app role can UPDATE balances, so the foreign key stops allow_negative drifting."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        app_conn.execute(
            "UPDATE balances SET allow_negative = true WHERE account_id = %s", (account,)
        )


@pytest.mark.parametrize(
    "values",
    [(False, None), (None, 0)],
    ids=["balance_minor", "allow_negative"],
)
def test_columns_are_not_null(
    clean_db: None, app_conn: psycopg.Connection, values: tuple[bool | None, int | None]
) -> None:
    """A NULL allow_negative would skip the foreign key (the default MATCH SIMPLE) and pass the
    CHECK (NULL OR ...), so a protected account could go negative through it."""
    account = insert_account(app_conn)
    allow_negative, balance = values
    with pytest.raises(psycopg.errors.NotNullViolation):
        app_conn.execute(INSERT_BALANCE, (account, allow_negative, balance))


@pytest.mark.parametrize("balance", [0, 1, 99_900_000_000, BIGINT_MAX])
def test_protected_account_can_hold_zero_or_a_positive_balance(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, balance))
    assert balance_of(app_conn, account) == balance


@pytest.mark.parametrize("balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_protected_account_cannot_start_negative(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    account = insert_account(app_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(INSERT_BALANCE, (account, False, balance))


@pytest.mark.parametrize("new_balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_update_cannot_push_a_protected_account_below_zero(
    clean_db: None, app_conn: psycopg.Connection, new_balance: int
) -> None:
    """Invariant: no protected account goes negative. The transfer code checks first; this CHECK
    is the backstop that makes a bug there impossible to commit."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 5_000))
    with pytest.raises(psycopg.errors.CheckViolation):
        app_conn.execute(
            "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (new_balance, account)
        )


def test_protected_balance_can_be_updated_to_another_non_negative_value(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 5_000))
    app_conn.execute("UPDATE balances SET balance_minor = 0 WHERE account_id = %s", (account,))
    assert balance_of(app_conn, account) == 0


@pytest.mark.parametrize("balance", [-1, -99_900_000_000, -BIGINT_MAX])
def test_funding_account_may_go_negative(
    clean_db: None, app_conn: psycopg.Connection, balance: int
) -> None:
    """Funding accounts issue money, so they go negative by exactly what they have issued."""
    funding = insert_account(app_conn, "funding:usd", allow_negative=True)
    app_conn.execute(INSERT_BALANCE, (funding, True, 0))
    app_conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (balance, funding)
    )
    assert balance_of(app_conn, funding) == balance


@pytest.mark.parametrize("statement", ["DELETE FROM balances", "TRUNCATE balances"])
def test_balances_cannot_be_deleted_by_the_app_role(
    clean_db: None, app_conn: psycopg.Connection, statement: str
) -> None:
    """Every account must keep its row, because every transfer locks it. Rows are never removed."""
    account = insert_account(app_conn)
    app_conn.execute(INSERT_BALANCE, (account, False, 0))
    app_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)


def migration_before_balances(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> tuple[str, Path]:
    """Apply migrations 001 to 003 to the scratch schema, as they exist on a real database before
    the balances migration runs. Returns the 004 file's name and path, not yet applied."""
    migrations = sorted(settings.migrations_dir.glob("*.sql"))
    before = [m for m in migrations if int(m.name.split("_", 1)[0]) < 4]
    balances = [m for m in migrations if int(m.name.split("_", 1)[0]) == 4]
    assert len(before) == 3, "expected migrations 001 to 003 to exist"
    assert len(balances) == 1, "the balances migration (004) does not exist yet"
    for m in before:
        (tmp_path / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    run_migrations(scratch_url, tmp_path)
    return balances[0].name, balances[0]


def seed(scratch_url: str, accounts: list[tuple[str, bool]], postings: list[list[tuple[int, int]]]):
    """Insert USD accounts (ids 1, 2, ... in order) and one transaction per posting, where each
    posting is a list of (account_id, amount_minor) entries that sum to zero."""
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        for name, allow_negative in accounts:
            conn.execute(
                "INSERT INTO accounts (name, currency, allow_negative) VALUES (%s, 'USD', %s)",
                (name, allow_negative),
            )
        for n, entries in enumerate(postings, start=1):
            assert sum(amount for _, amount in entries) == 0
            conn.execute(
                "INSERT INTO transactions (idempotency_key, request_hash) VALUES (%s, 'h')",
                (f"k{n}",),
            )
            for account_id, amount in entries:
                conn.execute(
                    "INSERT INTO entries (transaction_id, account_id, amount_minor, currency)"
                    " VALUES (%s, %s, %s, 'USD')",
                    (n, account_id, amount),
                )


def apply_balances_migration(scratch_url: str, tmp_path: Path, name: str, path: Path) -> list[str]:
    (tmp_path / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return run_migrations(scratch_url, tmp_path)


def backfilled(scratch_url: str) -> list[tuple[int, bool, int]]:
    with psycopg.connect(scratch_url) as conn:
        return conn.execute(
            "SELECT account_id, allow_negative, balance_minor FROM balances ORDER BY account_id"
        ).fetchall()


def test_backfill_sums_each_accounts_entries_and_copies_allow_negative(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """The migration runs on a database that already has accounts and entries."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[("customer:alice", False), ("funding:usd", True)],
        postings=[[(2, -5000), (1, 5000)], [(1, -1200), (2, 1200)]],  # fund alice, alice pays back
    )
    assert apply_balances_migration(scratch_url, tmp_path, name, path) == [name]
    assert backfilled(scratch_url) == [(1, False, 3800), (2, True, -3800)]


def test_backfill_gives_an_account_without_entries_a_zero_balance(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """Every account needs a row to lock, including one that has never been used, and one whose
    entries net to exactly zero."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[("customer:unused", False), ("customer:netzero", False), ("funding:usd", True)],
        postings=[[(3, -100), (2, 100)], [(2, -100), (3, 100)]],  # netzero: +100 then -100
    )
    apply_balances_migration(scratch_url, tmp_path, name, path)
    assert backfilled(scratch_url) == [(1, False, 0), (2, False, 0), (3, True, 0)]


def test_backfill_lets_a_funding_account_be_negative(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[("funding:usd", True), ("customer:alice", False)],
        postings=[[(1, -250_000), (2, 250_000)]],
    )
    apply_balances_migration(scratch_url, tmp_path, name, path)
    assert backfilled(scratch_url) == [(1, True, -250_000), (2, False, 250_000)]


def test_backfill_fails_readably_when_a_protected_account_would_be_negative(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """Existing entries that leave a protected account below zero mean the data already breaks an
    invariant. The migration must stop with a message that says so and names the account, not with
    the CHECK's generic error, and must leave nothing behind."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[
            ("customer:alice", False),  # id 1: fine
            ("customer:bob", False),  # id 2: ends at -700, the problem
            ("customer:carol", False),  # id 3: fine
            ("funding:usd", True),  # id 4
        ],
        postings=[[(4, -5000), (1, 5000)], [(2, -700), (3, 700)]],
    )

    with pytest.raises(psycopg.errors.RaiseException) as failure:
        apply_balances_migration(scratch_url, tmp_path, name, path)

    message = failure.value.diag.message_primary or ""
    assert "negative" in message.lower()
    assert re.search(r"(?<!\d)2(?!\d)", message), f"the message should name account 2: {message!r}"

    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT to_regclass('balances')").fetchone() == (None,)
        recorded = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    assert name not in [r[0] for r in recorded]


def numbers_in(message: str) -> set[int]:
    return {int(n) for n in re.findall(r"[0-9]+", message)}


def test_backfill_names_every_offender_and_the_count_when_there_are_few(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """One run should show the whole problem, so the operator is not left fixing accounts one
    migration attempt at a time."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[
            ("customer:alice", False),  # id 1: fine
            ("customer:bob", False),  # id 2: fine
            ("customer:carol", False),  # id 3: ends at -100, a problem
            ("customer:dave", False),  # id 4: ends at -200, a problem
            ("funding:usd", True),  # id 5
        ],
        postings=[[(3, -100), (5, 100)], [(4, -200), (5, 200)]],
    )

    with pytest.raises(psycopg.errors.RaiseException) as failure:
        apply_balances_migration(scratch_url, tmp_path, name, path)

    numbers = numbers_in(failure.value.diag.message_primary or "")
    assert {3, 4} <= numbers, "both offending accounts should be named"
    assert 2 in numbers, "the total number of offending accounts should be stated"
    assert not {1, 5} & numbers, "accounts that are fine must not be named"


def test_backfill_caps_the_listed_accounts_but_still_reports_the_total(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """A badly broken database must not produce an unreadable message: at most 20 accounts are
    listed, in id order, and the total says how many there really are."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    clean, offenders = 30, 23  # ids 1-30 are fine, ids 31-53 go negative, id 54 is the funder
    funding = clean + offenders + 1
    seed(
        scratch_url,
        accounts=[(f"customer:clean{i}", False) for i in range(clean)]
        + [(f"customer:bad{i}", False) for i in range(offenders)]
        + [("funding:usd", True)],
        postings=[[(clean + 1 + i, -100), (funding, 100)] for i in range(offenders)],
    )

    with pytest.raises(psycopg.errors.RaiseException) as failure:
        apply_balances_migration(scratch_url, tmp_path, name, path)

    numbers = numbers_in(failure.value.diag.message_primary or "")
    listed = {n for n in numbers if clean < n <= clean + offenders}
    assert listed == set(range(clean + 1, clean + 21)), "the first 20 offenders, in id order"
    assert offenders in numbers, "the total number of offenders should be stated"


def test_backfill_failure_shows_each_offenders_projected_balance(
    settings: Settings, scratch_url: str, tmp_path: Path
) -> None:
    """The operator needs to know by how much each account would be overdrawn, not just which
    accounts. Each offender's id must be followed by its own balance; the punctuation is free."""
    name, path = migration_before_balances(settings, scratch_url, tmp_path)
    seed(
        scratch_url,
        accounts=[
            ("customer:alice", False),  # id 1: ends at +5000, fine
            ("customer:bob", False),  # id 2: ends at -700
            ("customer:carol", False),  # id 3: ends at -1250
            ("funding:usd", True),  # id 4
        ],
        postings=[[(4, -5000), (1, 5000)], [(2, -700), (4, 700)], [(3, -1250), (4, 1250)]],
    )

    with pytest.raises(psycopg.errors.RaiseException) as failure:
        apply_balances_migration(scratch_url, tmp_path, name, path)

    message = failure.value.diag.message_primary or ""
    for account_id, balance in [(2, "-700"), (3, "-1250")]:
        pattern = f"(?<![0-9]){account_id}[^0-9-]*{balance}(?![0-9])"
        assert re.search(pattern, message), f"account {account_id} should be shown with {balance}"
    assert "5000" not in message, "a healthy account's balance must not appear"
