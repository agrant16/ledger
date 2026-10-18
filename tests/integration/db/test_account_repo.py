"""account_repo (ledger/repository/account_repo.py; ADR 0013): the SQL for accounts and balances.

    insert_account(conn, account_name, currency, allow_negative) -> Account
    insert_balance(conn, account_id, allow_negative) -> None
    find_balance(conn, account_id) -> a Balance-shaped row, or None
    find_account_by_name(conn, account_name) -> Account | None

Plain functions that run one statement on the connection they are given and return what the
database says. They never judge it: "there is no such account" is `None`, and "the account has no
balances row" is a row whose balance_minor is None; the service decides what each means. They do
translate database errors, so no raw integrity error leaves the layer (ADR 0013):

- a duplicate account name raises DuplicateAccountNameError (an expected outcome, a LedgerError);
- any other integrity error raises LedgerInvariantError (a bug, never a LedgerError) with the
  psycopg error as its __cause__.

They do not commit and do not check for a transaction: the service does.
"""

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ledger.errors import DuplicateAccountNameError, LedgerError, LedgerInvariantError
from ledger.models.account_model import Account, Balance


@pytest.fixture
def repo():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.repository import account_repo

    return account_repo


def count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


def shape(row) -> tuple:
    """The three fields of a balance row, whatever class carries them."""
    return (row.account_id, row.currency, row.balance_minor)


def set_cached_balance(conn: psycopg.Connection, account_id: int, balance_minor: int) -> None:
    conn.execute(
        "UPDATE balances SET balance_minor = %s WHERE account_id = %s", (balance_minor, account_id)
    )


# --- insert_account ---------------------------------------------------------------------------


def test_insert_account_returns_the_new_account(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "EUR", False)

    assert isinstance(account, Account)
    assert account.id >= 1
    assert (account.account_name, account.currency, account.allow_negative) == (
        "customer:alice",
        "EUR",
        False,
    )


def test_insert_account_writes_the_row_and_nothing_else(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The balances row is a separate step, so the service can decide when it is created."""
    account = repo.insert_account(app_conn, "customer:alice", "USD", True)

    row = app_conn.execute(
        "SELECT account_name, currency, allow_negative FROM accounts WHERE id = %s", (account.id,)
    ).fetchone()
    assert row == ("customer:alice", "USD", True)
    assert count(app_conn, "balances") == 0


def test_each_account_gets_a_new_id(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    first = repo.insert_account(app_conn, "customer:alice", "USD", False)
    second = repo.insert_account(app_conn, "customer:bob", "USD", False)

    assert second.id > first.id


def test_names_are_case_sensitive(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    repo.insert_account(app_conn, "customer:alice", "USD", False)
    repo.insert_account(app_conn, "Customer:Alice", "USD", False)


@pytest.mark.parametrize("currency", ["USD", "EUR"], ids=["same currency", "other currency"])
def test_a_duplicate_name_is_a_duplicate_account_name_error(
    clean_db: None, app_conn: psycopg.Connection, repo, currency: str
) -> None:
    """Names are unique across currencies."""
    repo.insert_account(app_conn, "customer:alice", "USD", False)

    with pytest.raises(DuplicateAccountNameError) as failure:
        with app_conn.transaction():  # a savepoint, so the first account survives the failure
            repo.insert_account(app_conn, "customer:alice", currency, False)

    assert isinstance(failure.value, LedgerError)
    assert failure.value.account_name == "customer:alice"
    assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)
    assert count(app_conn, "accounts") == 1


@pytest.mark.parametrize(
    "name, currency",
    [
        ("customer:alice", "usd"),
        ("customer:alice", "US"),
        ("", "USD"),
        (" padded", "USD"),
        ("x" * 101, "USD"),
    ],
    ids=["lowercase currency", "short currency", "empty name", "padded name", "long name"],
)
def test_a_value_the_database_refuses_is_a_bug_and_not_a_ledger_error(
    clean_db: None, app_conn: psycopg.Connection, repo, name: str, currency: str
) -> None:
    """Validation belongs to the API. A CHECK that fires means the API let something through."""
    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_account(app_conn, name, currency, False)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.CheckViolation)


# --- insert_balance ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow_negative", [False, True])
def test_insert_balance_writes_a_zero_row_copying_allow_negative(
    clean_db: None, app_conn: psycopg.Connection, repo, allow_negative: bool
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "USD", allow_negative)

    result = repo.insert_balance(app_conn, account.id, allow_negative)

    assert result is None
    row = app_conn.execute(
        "SELECT account_id, allow_negative, balance_minor FROM balances WHERE account_id = %s",
        (account.id,),
    ).fetchone()
    assert row == (account.id, allow_negative, 0)


def test_a_balance_for_an_unknown_account_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_balance(app_conn, 999_999, False)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.ForeignKeyViolation)


def test_a_balance_whose_allow_negative_differs_from_the_account_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_balance(app_conn, account.id, True)

    assert isinstance(failure.value.__cause__, psycopg.errors.ForeignKeyViolation)


def test_a_second_balance_row_for_an_account_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)
    repo.insert_balance(app_conn, account.id, False)

    with pytest.raises(LedgerInvariantError) as failure:
        with app_conn.transaction():
            repo.insert_balance(app_conn, account.id, False)

    assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)
    assert count(app_conn, "balances") == 1


# --- find_balance -----------------------------------------------------------------------------


def test_find_balance_returns_a_balance_for_an_account(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "EUR", False)
    repo.insert_balance(app_conn, account.id, False)

    result = repo.find_balance(app_conn, account.id)

    assert isinstance(result, Balance)
    assert result == Balance(account.id, "EUR", 0)


def test_find_balance_returns_the_cached_value_and_not_the_sum_of_the_entries(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """There are no entries at all here, so 500 can only come from the cache."""
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)
    repo.insert_balance(app_conn, account.id, False)
    set_cached_balance(app_conn, account.id, 500)

    assert repo.find_balance(app_conn, account.id).balance_minor == 500


def test_find_balance_can_return_a_negative_balance(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    funding = repo.insert_account(app_conn, "funding:usd", "USD", True)
    repo.insert_balance(app_conn, funding.id, True)
    set_cached_balance(app_conn, funding.id, -300)

    assert repo.find_balance(app_conn, funding.id).balance_minor == -300


def test_each_account_has_its_own_balance(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    alice = repo.insert_account(app_conn, "customer:alice", "USD", False)
    bob = repo.insert_account(app_conn, "customer:bob", "USD", False)
    repo.insert_balance(app_conn, alice.id, False)
    repo.insert_balance(app_conn, bob.id, False)
    set_cached_balance(app_conn, alice.id, 70)
    set_cached_balance(app_conn, bob.id, 80)

    assert repo.find_balance(app_conn, alice.id).balance_minor == 70
    assert repo.find_balance(app_conn, bob.id).balance_minor == 80


def test_find_balance_returns_none_for_an_unknown_account(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """Not an error at this layer: the query ran and found nothing."""
    assert repo.find_balance(app_conn, 999_999) is None


def test_an_account_with_no_balances_row_comes_back_as_it_is(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    """The repository does not know that every account should have a balances row. It reports the
    account and an empty balance, and the service decides that this is a bug."""
    account = repo.insert_account(app_conn, "orphan", "USD", False)

    result = repo.find_balance(app_conn, account.id)

    assert result is not None
    assert shape(result) == (account.id, "USD", None)


def test_find_balance_takes_no_row_locks(
    clean_db: None,
    app_conn: psycopg.Connection,
    app_pool: ConnectionPool,
    commit_setup,
    repo,
) -> None:
    """Another connection holds the balances row FOR UPDATE, as a transfer in flight would. A plain
    read must not wait for it: the reading connection refuses to wait more than 300 ms."""
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)
    repo.insert_balance(app_conn, account.id, False)
    commit_setup()

    with app_pool.connection() as holder:
        holder.execute("SELECT 1 FROM balances WHERE account_id = %s FOR UPDATE", (account.id,))

        app_conn.execute("SET LOCAL lock_timeout = '300ms'")
        result = repo.find_balance(app_conn, account.id)

    assert result.balance_minor == 0


def test_find_balance_changes_nothing(clean_db: None, app_conn: psycopg.Connection, repo) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)
    repo.insert_balance(app_conn, account.id, False)
    set_cached_balance(app_conn, account.id, 12)

    repo.find_balance(app_conn, account.id)
    repo.find_balance(app_conn, account.id)

    row = app_conn.execute(
        "SELECT balance_minor FROM balances WHERE account_id = %s", (account.id,)
    ).fetchone()
    assert row == (12,)
    assert (count(app_conn, "accounts"), count(app_conn, "balances")) == (1, 1)


# --- every integrity error is translated, not only the ones with a name ------------------------


@pytest.mark.parametrize(
    "name, currency, allow_negative",
    [(None, "USD", False), ("customer:alice", None, False), ("customer:alice", "USD", None)],
    ids=["no name", "no currency", "no allow_negative"],
)
def test_a_missing_value_is_a_bug_and_not_a_raw_database_error(
    clean_db: None,
    app_conn: psycopg.Connection,
    repo,
    name: str | None,
    currency: str | None,
    allow_negative: bool | None,
) -> None:
    """Any integrity error, not just a CHECK or the duplicate name, reaches the caller as a
    LedgerInvariantError. Here it is a NOT NULL violation."""
    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_account(app_conn, name, currency, allow_negative)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.NotNullViolation)


def test_a_balance_with_no_allow_negative_is_a_bug(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    account = repo.insert_account(app_conn, "customer:alice", "USD", False)

    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_balance(app_conn, account.id, None)

    assert isinstance(failure.value.__cause__, psycopg.errors.NotNullViolation)


# --- find_account_by_name (issue #62) ---------------------------------------------------------


def test_find_account_by_name_returns_the_account(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    created = repo.insert_account(app_conn, "funding:USD", "USD", True)

    found = repo.find_account_by_name(app_conn, "funding:USD")

    assert isinstance(found, Account)
    assert found == created


def test_find_account_by_name_returns_none_for_an_unknown_name(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    repo.insert_account(app_conn, "funding:USD", "USD", True)

    assert repo.find_account_by_name(app_conn, "funding:EUR") is None


def test_find_account_by_name_matches_exactly(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    repo.insert_account(app_conn, "funding:USD", "USD", True)

    assert repo.find_account_by_name(app_conn, "FUNDING:USD") is None
    assert repo.find_account_by_name(app_conn, "funding:USD%") is None  # not a LIKE pattern


def test_find_account_by_name_changes_nothing(
    clean_db: None, app_conn: psycopg.Connection, repo
) -> None:
    repo.insert_account(app_conn, "funding:USD", "USD", True)
    before = (count(app_conn, "accounts"), count(app_conn, "balances"))

    repo.find_account_by_name(app_conn, "funding:USD")
    repo.find_account_by_name(app_conn, "funding:EUR")

    assert (count(app_conn, "accounts"), count(app_conn, "balances")) == before
