"""insert_claim and find_claim (ledger/repository/posting_repo.py; ADR 0012, ADR 0013): the SQL of
the idempotency claim.

    insert_claim(conn, idempotency_key, request_hash, reverses_transaction_id) -> int | None
    find_claim(conn, idempotency_key) -> (transaction_id, request_hash), or None

`insert_claim` runs `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`: it returns
the new transaction's id, or None when the key is already taken, whatever the hash or the reversal
target. The conflict target is the key only, so a clash on the reversal link is not mistaken for a
replay and surfaces as an error. `find_claim` reads the stored row for a key. Neither judges
anything: whether a taken key is a replay or a conflict is the service's decision, from the hash.

Integrity errors are translated into a LedgerInvariantError chained to the psycopg error (ADR 0013).
Until milestone 5 maps the reversal link to AlreadyReversedError, that includes a second reversal
of the same transfer. Neither function commits or checks for a transaction.
"""

import psycopg
import pytest

from ledger.errors import LedgerError, LedgerInvariantError
from ledger.repository import posting_repo as repo
from tests.db_helpers import hash_for

HASH_A = hash_for("request a")
HASH_B = hash_for("request b")


def transaction_rows(conn: psycopg.Connection) -> list[tuple[int, str, str, int | None]]:
    return conn.execute(
        "SELECT id, idempotency_key, request_hash, reverses_transaction_id FROM transactions"
        " ORDER BY id"
    ).fetchall()


# --- insert_claim -----------------------------------------------------------------------------


def test_a_free_key_returns_the_new_transaction_id(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    first = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    second = repo.insert_claim(app_conn, "key-2", HASH_B, None)

    assert first is not None and first >= 1
    assert second is not None and second > first


def test_the_key_the_hash_and_the_reversal_target_are_stored(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    original = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    reversal = repo.insert_claim(app_conn, "key-2", HASH_B, original)

    assert transaction_rows(app_conn) == [
        (original, "key-1", HASH_A, None),
        (reversal, "key-2", HASH_B, original),
    ]


def test_a_taken_key_returns_none_whatever_the_hash(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    repo.insert_claim(app_conn, "key-1", HASH_A, None)

    assert repo.insert_claim(app_conn, "key-1", HASH_A, None) is None
    assert repo.insert_claim(app_conn, "key-1", HASH_B, None) is None


def test_a_taken_key_changes_nothing(clean_db: None, app_conn: psycopg.Connection) -> None:
    first = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    before = transaction_rows(app_conn)

    repo.insert_claim(app_conn, "key-1", HASH_B, None)
    repo.insert_claim(app_conn, "key-1", HASH_A, None)

    assert transaction_rows(app_conn) == before == [(first, "key-1", HASH_A, None)]


def test_a_taken_key_does_not_abort_the_transaction(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """A violated unique constraint would abort the whole database transaction. ON CONFLICT does
    not, so the caller goes on to read the stored claim in the same transaction."""
    repo.insert_claim(app_conn, "key-1", HASH_A, None)
    repo.insert_claim(app_conn, "key-1", HASH_A, None)

    assert app_conn.execute("SELECT 1").fetchone() == (1,)
    assert repo.insert_claim(app_conn, "key-2", HASH_B, None) is not None


def test_a_retried_reversal_with_the_same_key_returns_none_and_not_an_error(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """A retried reversal repeats both the key and the reversal link, and Postgres does not say
    which unique constraint it reports. The key conflict is resolved first."""
    original = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    repo.insert_claim(app_conn, "key-2", HASH_B, original)

    assert repo.insert_claim(app_conn, "key-2", HASH_B, original) is None


def test_keys_are_compared_exactly(clean_db: None, app_conn: psycopg.Connection) -> None:
    ids = {
        repo.insert_claim(app_conn, key, HASH_A, None)
        for key in ("key-1", "KEY-1", "key-1 ", " key-1", "key-1\N{NO-BREAK SPACE}")
    }

    assert None not in ids
    assert len(ids) == 5


def test_a_second_reversal_under_a_new_key_is_a_bug_until_milestone_5(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    """Only the key conflict is skipped. A different key aimed at an already reversed transfer
    fails on the reversal link. Milestone 5 turns this into AlreadyReversedError and updates this
    test."""
    original = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    repo.insert_claim(app_conn, "key-2", HASH_B, original)

    with pytest.raises(LedgerInvariantError) as failure:
        with app_conn.transaction():
            repo.insert_claim(app_conn, "key-3", hash_for("another"), original)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)


@pytest.mark.parametrize(
    "key, request_hash, reverses, cause",
    [
        ("", HASH_A, None, psycopg.errors.CheckViolation),
        ("x" * 256, HASH_A, None, psycopg.errors.CheckViolation),
        ("key-1", "not a hash", None, psycopg.errors.CheckViolation),
        ("key-1", HASH_A, 999_999, psycopg.errors.ForeignKeyViolation),
        (None, HASH_A, None, psycopg.errors.NotNullViolation),
        ("key-1", None, None, psycopg.errors.NotNullViolation),
    ],
    ids=[
        "empty key",
        "key over 255 characters",
        "malformed hash",
        "unknown reversal target",
        "no key",
        "no hash",
    ],
)
def test_a_value_the_database_refuses_is_a_bug_and_not_a_ledger_error(
    clean_db: None,
    app_conn: psycopg.Connection,
    key: str | None,
    request_hash: str | None,
    reverses: int | None,
    cause: type[Exception],
) -> None:
    with pytest.raises(LedgerInvariantError) as failure:
        repo.insert_claim(app_conn, key, request_hash, reverses)

    assert not isinstance(failure.value, LedgerError)
    assert isinstance(failure.value.__cause__, cause)


# --- find_claim -------------------------------------------------------------------------------


def test_find_claim_returns_the_stored_id_and_hash(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    repo.insert_claim(app_conn, "key-1", HASH_A, None)
    second = repo.insert_claim(app_conn, "key-2", HASH_B, None)

    found = repo.find_claim(app_conn, "key-2")

    assert found is not None
    transaction_id, request_hash = found
    assert (transaction_id, request_hash) == (second, HASH_B)


def test_find_claim_returns_none_for_a_key_nobody_used(
    clean_db: None, app_conn: psycopg.Connection
) -> None:
    repo.insert_claim(app_conn, "key-1", HASH_A, None)

    assert repo.find_claim(app_conn, "key-2") is None


def test_find_claim_compares_keys_exactly(clean_db: None, app_conn: psycopg.Connection) -> None:
    lower = repo.insert_claim(app_conn, "key-1", HASH_A, None)
    upper = repo.insert_claim(app_conn, "KEY-1", HASH_B, None)

    assert repo.find_claim(app_conn, "key-1") == (lower, HASH_A)
    assert repo.find_claim(app_conn, "KEY-1") == (upper, HASH_B)
    assert repo.find_claim(app_conn, "Key-1") is None


def test_find_claim_changes_nothing(clean_db: None, app_conn: psycopg.Connection) -> None:
    repo.insert_claim(app_conn, "key-1", HASH_A, None)
    before = transaction_rows(app_conn)

    repo.find_claim(app_conn, "key-1")
    repo.find_claim(app_conn, "key-2")

    assert transaction_rows(app_conn) == before
