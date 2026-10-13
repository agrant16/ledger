"""API validation is at least as strict as the database (docs/design.md, Error behavior).

A request the API accepts must never make the database complain, because that would be a bug that
surfaces as a logged 500 instead of a clean 400. The API may be stricter than the database, but
never looser. So for arbitrary values:

    if the API accepts a value, storing it must work.

The values include the nasty ones: NUL characters and lone surrogates (neither is a CHECK, but
Postgres and psycopg both refuse them), a trailing newline that Python's regex $ would let through
and Postgres's would not, whitespace of every kind, and numbers just outside the bigint range.

Each example runs in a transaction that is always rolled back. The test also counts how many
values the API accepted, so it cannot pass just because the API rejected everything.
"""

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.usefixtures("migrated_database")

# Every CHECK constraint in the schema, and who is responsible for the values it guards. A new
# CHECK makes test_every_check_constraint_is_accounted_for fail until it is added here.
CHECKS = {
    "accounts_account_name_length": "request-derived: AccountName",
    "accounts_account_name_whitespace": "request-derived: AccountName",
    "accounts_currency_format": "request-derived: Currency",
    "transactions_idempotency_key_length": "request-derived: IdempotencyKey",
    "transactions_request_hash_format": "service rule: request_hash() always makes 64 hex digits",
    "entries_amount_minor_not_zero": "request-derived: AmountMinor",
    "balances_protected_nonnegative": "service rule: the overdraft check in post_transaction",
}

# Any character at all, including NUL and the surrogate code points.
ANY_CHARACTER = st.characters(exclude_categories=())
WHITESPACE_AND_LOOKALIKES = st.sampled_from(
    list(" \t\n\v\f\r\N{NO-BREAK SPACE}\N{EM SPACE}\u0085\x1c\x00") + ["a", "b", "é", "\U0001f600"]
)

NAMES = st.one_of(
    st.text(alphabet=ANY_CHARACTER, max_size=130),
    st.text(alphabet=WHITESPACE_AND_LOOKALIKES, max_size=105),
    st.integers(1, 130).map(lambda n: "x" * n),
    st.integers(1, 130).map(lambda n: "\U0001f600" * n),
)
CURRENCIES = st.one_of(
    st.text(alphabet=ANY_CHARACTER, max_size=5),
    st.text(alphabet=st.sampled_from(list("USDusd\n \x00Ü")), max_size=5),
    st.from_regex(r"[A-Z]{3}", fullmatch=True),
)
KEYS = st.one_of(
    st.text(alphabet=ANY_CHARACTER, max_size=300),
    st.integers(1, 300).map(lambda n: "k" * n),
)
BIG_INTEGERS = st.one_of(
    st.integers(-(2**70), 2**70),
    st.sampled_from([0, 1, -1, 2**63 - 1, 2**63, 99_900_000_000, 99_900_000_001]),
)

SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def conn(settings):
    """The app role, as the service connects. Autocommit, so each example controls its own
    transaction and always rolls it back."""
    with psycopg.connect(settings.app_url, autocommit=True) as connection:
        yield connection


def stored_without_complaint(conn: psycopg.Connection, sql: str, params: tuple) -> None:
    with conn.transaction(force_rollback=True):
        conn.execute(sql, params)


def count_accepted(conn, strategy, accepts, store, minimum: int) -> None:
    """Run the property and check that enough generated values were accepted for it to mean
    something."""
    accepted: list = []

    @SETTINGS
    @given(value=strategy)
    def check(value):
        if accepts(value):
            accepted.append(value)
            store(conn, value)

    check()
    assert len(accepted) >= minimum, f"only {len(accepted)} values reached the database"


def accepted_by(adapter: TypeAdapter):
    def accepts(value) -> bool:
        try:
            adapter.validate_python(value)
        except ValidationError:
            return False
        return True

    return accepts


def test_an_account_name_the_api_accepts_can_be_stored(conn) -> None:
    from ledger.api.accounts import CreateAccountRequest

    def accepts(name) -> bool:
        try:
            CreateAccountRequest.model_validate({"account_name": name, "currency": "USD"})
        except ValidationError:
            return False
        return True

    def store(connection, name) -> None:
        stored_without_complaint(
            connection,
            "INSERT INTO accounts (account_name, currency, allow_negative)"
            " VALUES (%s, 'USD', false)",
            (name,),
        )

    count_accepted(conn, NAMES, accepts, store, minimum=40)


def test_a_currency_the_api_accepts_can_be_stored(conn) -> None:
    from ledger.api.accounts import CreateAccountRequest

    def accepts(currency) -> bool:
        try:
            CreateAccountRequest.model_validate({"account_name": "a", "currency": currency})
        except ValidationError:
            return False
        return True

    def store(connection, currency) -> None:
        stored_without_complaint(
            connection,
            "INSERT INTO accounts (account_name, currency, allow_negative) VALUES ('a', %s, false)",
            (currency,),
        )

    count_accepted(conn, CURRENCIES, accepts, store, minimum=20)


def test_an_idempotency_key_the_api_accepts_can_be_stored(conn) -> None:
    from ledger.api.types import IdempotencyKey

    def store(connection, key) -> None:
        stored_without_complaint(
            connection,
            "INSERT INTO transactions (idempotency_key, request_hash) VALUES (%s, %s)",
            (key, "a" * 64),
        )

    count_accepted(conn, KEYS, accepted_by(TypeAdapter(IdempotencyKey)), store, minimum=40)


def test_an_amount_the_api_accepts_is_a_nonzero_bigint(conn) -> None:
    from ledger.api.types import AmountMinor

    def store(connection, amount) -> None:
        row = connection.execute("SELECT %s::bigint <> 0", (amount,)).fetchone()
        assert row == (True,)

    count_accepted(conn, BIG_INTEGERS, accepted_by(TypeAdapter(AmountMinor)), store, minimum=20)


def test_an_account_id_the_api_accepts_is_a_positive_bigint(conn) -> None:
    from ledger.api.types import AccountId

    def store(connection, account_id) -> None:
        row = connection.execute("SELECT %s::bigint >= 1", (account_id,)).fetchone()
        assert row == (True,)

    count_accepted(conn, BIG_INTEGERS, accepted_by(TypeAdapter(AccountId)), store, minimum=20)


def test_every_check_constraint_is_accounted_for(conn) -> None:
    """Add a row to CHECKS, and cover the new rule above, when a migration adds a CHECK."""
    rows = conn.execute(
        "SELECT conname FROM pg_constraint"
        " WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
    ).fetchall()
    assert {name for (name,) in rows} == set(CHECKS)
