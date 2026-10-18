"""The seed script, `python -m ledger.seed` (issue #62; docs/design.md, Seed money; Correctness
rule 6 for the currencies).

    python -m ledger.seed --output PATH [--currency CODE ...] [--per-currency N]

It creates the funding accounts (`allow_negative` true) that money enters the ledger through, as the
app role and through `create_account`, and writes their ids to a JSON file:

    { "funding_accounts": { "USD": [1], "EUR": [2] } }

Each currency maps to its account ids in index order. The first account of a currency is named
`funding:<CURRENCY>`, the rest `funding:<CURRENCY>:2`, `:3` and so on. The defaults are USD and
EUR, one account each.

The script is safe to run twice, and two runs at once. It looks a name up before creating it (a
failed insert still uses up an identity value, so ids would drift on every re-run), and creates
inside a savepoint (a unique violation would otherwise abort the whole database transaction when two
runs race). An existing account is skipped only if it is what the script would have created;
otherwise the run fails with exit status 1, creates nothing and writes no file.

Most tests call `main(argv)` in this process; a few run `python -m ledger.seed` as a separate
process, which is how the end-to-end tests will use it. The script commits its own data, so every
test resets the database afterwards. Argument errors are in tests/unit/test_seed_args.py.
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from ledger.service.account_service import create_account
from tests.db_helpers import reset_database

SRC = Path(__file__).resolve().parents[3] / "src"
WAIT_SECONDS = 10
# Credentials the owner role can never have. A script that touched the owner role would fail.
UNUSABLE_OWNER_URL = "postgresql://ledger_owner:wrong@127.0.0.1:1/nowhere?connect_timeout=1"


@pytest.fixture
def seed():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger import seed as module

    return module


@pytest.fixture
def fresh_db(owner_conn: psycopg.Connection):
    """Empty tables before the test and again after it, because the script commits."""
    reset_database(owner_conn)
    yield
    reset_database(owner_conn)


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "ids.json"


def run(seed, output: Path, *args: str) -> int:
    return seed.main(["--output", str(output), *args])


def run_as_process(*args: str, cwd: Path, owner_url: str = UNUSABLE_OWNER_URL):
    """`python -m ledger.seed` in its own process, with the test database in its environment
    (the session fixture put it in os.environ) and an owner URL that cannot connect."""
    env = {**os.environ, "PYTHONPATH": str(SRC), "DATABASE_OWNER_URL": owner_url}
    return subprocess.run(
        [sys.executable, "-m", "ledger.seed", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
    )


def accounts(conn: psycopg.Connection) -> list[tuple]:
    """(id, account_name, currency, allow_negative) of every account, by id."""
    return conn.execute(
        "SELECT id, account_name, currency, allow_negative FROM accounts ORDER BY id"
    ).fetchall()


def ids_by_name(conn: psycopg.Connection) -> dict[str, int]:
    return {row[1]: row[0] for row in accounts(conn)}


def read_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_names(currency: str, count: int) -> list[str]:
    return [f"funding:{currency}" + (f":{i}" if i > 1 else "") for i in range(1, count + 1)]


def wait_for_a_blocked_backend(conn: psycopg.Connection) -> None:
    """Wait until some other session has a lock request that is not granted. pg_locks is readable
    by any role, so the owner connection can look at the app role's sessions."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid <> pg_backend_pid()"
        ).fetchone()
        if row is not None and row[0] > 0:
            return
        time.sleep(0.01)
    raise AssertionError("the seed script never waited for a lock: it did not reach the insert")


# --- what a run creates -----------------------------------------------------------------------


def test_a_default_run_creates_a_funding_account_for_usd_and_for_eur(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    assert run(seed, output) == 0

    rows = {row[1]: row[2:] for row in accounts(owner_conn)}
    assert rows == {"funding:USD": ("USD", True), "funding:EUR": ("EUR", True)}


def test_every_funding_account_starts_with_a_zero_balance_and_no_entries(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    """The script moves no money: it only creates the accounts the transfers will draw on."""
    run(seed, output)

    balances = owner_conn.execute(
        "SELECT b.account_id, b.allow_negative, b.balance_minor FROM balances b ORDER BY 1"
    ).fetchall()
    assert balances == [(row[0], True, 0) for row in accounts(owner_conn)]
    for table in ("transactions", "entries"):
        assert owner_conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_per_currency_creates_numbered_accounts_for_each_currency(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    args = ("--currency", "USD", "--currency", "GBP", "--per-currency", "3")

    assert run(seed, output, *args) == 0

    names = set(ids_by_name(owner_conn))
    assert names == set(expected_names("USD", 3) + expected_names("GBP", 3))
    assert all(row[3] is True for row in accounts(owner_conn))


def test_naming_a_currency_replaces_the_defaults(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    assert run(seed, output, "--currency", "GBP") == 0

    assert set(ids_by_name(owner_conn)) == {"funding:GBP"}
    assert set(read_file(output)["funding_accounts"]) == {"GBP"}


@pytest.mark.parametrize(
    "currencies",
    [
        ["USD", "EUR", "GBP"],
        ["EUR", "USD", "GBP"],
        ["GBP", "USD", "EUR"],
        ["USD", "EUR", "GBP", "JPY"],
        ["USD", "EUR", "USD", "EUR", "GBP"],
    ],
    ids=lambda currencies: " ".join(currencies),
)
def test_every_named_currency_is_seeded_however_many_are_given(
    fresh_db: None,
    seed,
    output: Path,
    owner_conn: psycopg.Connection,
    currencies: list[str],
) -> None:
    """Naming the two defaults first, in their default order, must not make the later currencies
    replace them: the list a repeated option builds is not the default list, even when it is equal
    to it. Repeats are dropped and the first-seen order is kept."""
    args = [arg for currency in currencies for arg in ("--currency", currency)]
    wanted = list(dict.fromkeys(currencies))

    assert run(seed, output, *args) == 0

    assert set(ids_by_name(owner_conn)) == {f"funding:{currency}" for currency in wanted}
    assert list(read_file(output)["funding_accounts"]) == wanted


def test_a_count_with_a_zero_digit_is_accepted(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    """10 and 20 are ordinary counts; a check for digits 1 to 9 only would reject them."""
    assert run(seed, output, "--currency", "USD", "--per-currency", "10") == 0

    assert set(ids_by_name(owner_conn)) == set(expected_names("USD", 10))


def test_the_same_currency_named_twice_makes_its_accounts_once(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    """A repeated --currency is deduplicated, not an error and not a second set of accounts."""
    assert run(seed, output, "--currency", "USD", "--currency", "USD") == 0

    assert set(ids_by_name(owner_conn)) == {"funding:USD"}
    assert len(read_file(output)["funding_accounts"]["USD"]) == 1


# --- the ids file -----------------------------------------------------------------------------


def test_the_file_has_the_documented_shape(fresh_db: None, seed, output: Path) -> None:
    run(seed, output, "--per-currency", "2")

    content = read_file(output)
    assert set(content) == {"funding_accounts"}
    assert set(content["funding_accounts"]) == {"USD", "EUR"}
    for ids in content["funding_accounts"].values():
        assert isinstance(ids, list) and len(ids) == 2
        assert all(type(i) is int for i in ids)


def test_the_file_lists_each_currencys_ids_in_index_order_and_matches_the_database(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    run(seed, output, "--currency", "USD", "--currency", "EUR", "--per-currency", "3")

    by_name = ids_by_name(owner_conn)
    expected = {
        currency: [by_name[name] for name in expected_names(currency, 3)]
        for currency in ("USD", "EUR")
    }
    assert read_file(output) == {"funding_accounts": expected}


def test_the_file_is_written_whole_and_leaves_no_temporary_file_behind(
    fresh_db: None, seed, output: Path
) -> None:
    run(seed, output)

    assert [path.name for path in output.parent.iterdir()] == [output.name]


# --- running it again -------------------------------------------------------------------------


def test_a_second_run_creates_nothing_and_writes_the_same_file(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    run(seed, output)
    first_accounts, first_file = accounts(owner_conn), output.read_bytes()

    assert run(seed, output) == 0

    assert accounts(owner_conn) == first_accounts
    assert output.read_bytes() == first_file
    assert owner_conn.execute("SELECT count(*) FROM balances").fetchone() == (len(first_accounts),)


def test_a_second_run_does_not_use_up_identity_values(
    fresh_db: None, seed, output: Path, app_conn: psycopg.Connection
) -> None:
    """The names are looked up before they are created. Trying to insert a name that exists fails
    but still advances the id counter, so ids would drift on every re-run (and the ids a developer
    sees after `reset_db.py --seed` would no longer be predictable)."""
    run(seed, output)
    run(seed, output)

    next_account = create_account(app_conn, "customer:next", "USD", False)

    assert next_account.id == 3  # funding:USD and funding:EUR took 1 and 2


def test_a_run_reports_the_ids_of_accounts_that_already_exist(
    fresh_db: None, seed, tmp_path: Path
) -> None:
    """A different output file on the second run still lists the ids from the first."""
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    run(seed, first)

    run(seed, second)

    assert read_file(second) == read_file(first)


def test_only_the_missing_accounts_are_created(
    fresh_db: None,
    seed,
    output: Path,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
) -> None:
    existing = create_account(app_conn, "funding:USD", "USD", True)
    app_conn.commit()

    assert run(seed, output) == 0

    assert read_file(output)["funding_accounts"]["USD"] == [existing.id]
    assert set(ids_by_name(owner_conn)) == {"funding:USD", "funding:EUR"}
    assert ids_by_name(owner_conn)["funding:USD"] == existing.id


def test_a_larger_count_adds_accounts_and_keeps_the_ones_it_has(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    run(seed, output)
    before = ids_by_name(owner_conn)

    assert run(seed, output, "--per-currency", "2") == 0

    after = ids_by_name(owner_conn)
    assert set(after) == set(expected_names("USD", 2) + expected_names("EUR", 2))
    assert {name: after[name] for name in before} == before
    assert read_file(output)["funding_accounts"]["USD"] == [
        after["funding:USD"],
        after["funding:USD:2"],
    ]


def test_a_matching_account_in_the_middle_is_skipped_and_the_gaps_are_filled(
    fresh_db: None,
    seed,
    output: Path,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
) -> None:
    """Only funding:USD:3 exists, and it is what the script would have made. Asking for five makes
    the other four, keeps the existing one and lists all five ids in index order."""
    existing = create_account(app_conn, "funding:USD:3", "USD", True)
    app_conn.commit()

    assert run(seed, output, "--currency", "USD", "--per-currency", "5") == 0

    by_name = ids_by_name(owner_conn)
    assert set(by_name) == set(expected_names("USD", 5))
    assert by_name["funding:USD:3"] == existing.id
    assert read_file(output) == {
        "funding_accounts": {"USD": [by_name[name] for name in expected_names("USD", 5)]}
    }


def test_when_every_requested_account_exists_their_ids_are_reported_and_nothing_is_created(
    fresh_db: None, seed, tmp_path: Path, owner_conn: psycopg.Connection
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    args = ("--currency", "USD", "--per-currency", "5")
    run(seed, first, *args)
    created = accounts(owner_conn)

    assert run(seed, second, *args) == 0

    assert accounts(owner_conn) == created
    assert read_file(second) == read_file(first)
    assert read_file(second)["funding_accounts"]["USD"] == [row[0] for row in created]


def test_a_smaller_count_than_exists_reports_only_the_requested_accounts(
    fresh_db: None, seed, output: Path, owner_conn: psycopg.Connection
) -> None:
    """Asking for fewer than exist neither deletes nor lists the extra ones."""
    run(seed, output, "--currency", "USD", "--per-currency", "5")
    before = ids_by_name(owner_conn)

    assert run(seed, output, "--currency", "USD", "--per-currency", "2") == 0

    assert ids_by_name(owner_conn) == before
    assert read_file(output)["funding_accounts"]["USD"] == [
        before["funding:USD"],
        before["funding:USD:2"],
    ]


# --- an existing account that is not what the script would have made --------------------------


@pytest.mark.parametrize(
    "currency, allow_negative",
    [("USD", True), ("EUR", False), ("USD", False)],
    ids=["wrong currency", "protected", "wrong currency and protected"],
)
def test_an_existing_account_of_the_wrong_kind_fails_the_run_and_changes_nothing(
    fresh_db: None,
    seed,
    output: Path,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    currency: str,
    allow_negative: bool,
) -> None:
    """Anyone can create an account called `funding:EUR` through the API, and it will be protected.
    Reporting it as a funding account would only fail later, in a transfer. It is the second
    currency here so that the first one's account has to be rolled back too."""
    squatter = create_account(app_conn, "funding:EUR", currency, allow_negative)
    app_conn.commit()

    assert run(seed, output) == 1

    assert accounts(owner_conn) == [(squatter.id, "funding:EUR", currency, allow_negative)]
    assert not output.exists()


def test_a_failed_run_leaves_an_existing_output_file_alone(
    fresh_db: None, seed, output: Path, app_conn: psycopg.Connection
) -> None:
    create_account(app_conn, "funding:USD", "USD", False)
    app_conn.commit()
    output.write_text('{"keep": "me"}', encoding="utf-8")

    assert run(seed, output) == 1

    assert output.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_a_failed_run_leaves_no_temporary_file_behind(
    fresh_db: None, seed, output: Path, app_conn: psycopg.Connection
) -> None:
    create_account(app_conn, "funding:USD", "USD", False)
    app_conn.commit()

    run(seed, output)

    assert list(output.parent.iterdir()) == []


# --- the role ---------------------------------------------------------------------------------


def test_it_connects_as_the_app_role_and_never_as_the_owner(
    fresh_db: None, seed, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an owner URL that cannot connect, a script that touched the owner role would fail."""
    monkeypatch.setenv("DATABASE_OWNER_URL", UNUSABLE_OWNER_URL)

    assert run(seed, output) == 0


# --- as a separate process --------------------------------------------------------------------


def test_run_as_a_module_it_creates_the_accounts_and_reports_them(
    fresh_db: None, tmp_path: Path, owner_conn: psycopg.Connection
) -> None:
    output = tmp_path / "ids.json"

    result = run_as_process("--output", str(output), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    by_name = ids_by_name(owner_conn)
    assert set(by_name) == {"funding:USD", "funding:EUR"}
    assert read_file(output) == {
        "funding_accounts": {"USD": [by_name["funding:USD"]], "EUR": [by_name["funding:EUR"]]}
    }
    for name in by_name:
        assert name in result.stdout


def test_run_as_a_module_a_conflicting_account_exits_1_and_names_it(
    fresh_db: None, tmp_path: Path, app_conn: psycopg.Connection
) -> None:
    create_account(app_conn, "funding:EUR", "EUR", False)
    app_conn.commit()
    output = tmp_path / "ids.json"

    result = run_as_process("--output", str(output), cwd=tmp_path)

    assert result.returncode == 1
    assert "funding:EUR" in result.stdout + result.stderr
    assert not output.exists()


def test_run_as_a_module_a_usage_error_exits_2(fresh_db: None, tmp_path: Path) -> None:
    result = run_as_process(cwd=tmp_path)

    assert result.returncode == 2
    assert "--output" in result.stderr


# --- two runs at once -------------------------------------------------------------------------


def test_losing_a_race_to_an_identical_account_counts_as_already_there(
    fresh_db: None,
    seed,
    output: Path,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
) -> None:
    """Another run has created `funding:USD` but not yet committed it. The lookup cannot see it, so
    the script goes on to insert, and waits on the other transaction's unique entry. When that
    commits, the insert fails as a duplicate; the savepoint keeps the run alive, the name is looked
    up again, and it is the account the script wanted, so the run succeeds."""
    winner = create_account(app_conn, "funding:USD", "USD", True)  # not committed yet
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, seed, output)
        try:
            wait_for_a_blocked_backend(owner_conn)
            app_conn.commit()
        finally:
            app_conn.rollback()  # a no-op after the commit; frees the script if the wait failed
        assert future.result(timeout=WAIT_SECONDS) == 0

    assert read_file(output)["funding_accounts"]["USD"] == [winner.id]
    names = [row[1] for row in accounts(owner_conn)]
    assert sorted(names) == ["funding:EUR", "funding:USD"]


def test_losing_a_race_to_a_different_kind_of_account_fails_the_run(
    fresh_db: None,
    seed,
    output: Path,
    app_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
) -> None:
    """The same race, but the account that wins is a protected one: after the duplicate the second
    lookup shows it is not a funding account, so the run fails and creates nothing."""
    winner = create_account(app_conn, "funding:USD", "USD", False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, seed, output)
        try:
            wait_for_a_blocked_backend(owner_conn)
            app_conn.commit()
        finally:
            app_conn.rollback()
        assert future.result(timeout=WAIT_SECONDS) == 1

    assert accounts(owner_conn) == [(winner.id, "funding:USD", "USD", False)]
    assert not output.exists()


def test_two_runs_released_together_both_succeed_with_the_same_ids(
    fresh_db: None, seed, tmp_path: Path, owner_conn: psycopg.Connection
) -> None:
    """A sample of the interleaving, not proof that the runs overlapped: the two tests above are
    the deterministic ones. Whatever the order, both succeed and the accounts exist once."""
    outputs = [tmp_path / "a.json", tmp_path / "b.json"]
    barrier = threading.Barrier(2, timeout=WAIT_SECONDS)

    def release_and_run(path: Path) -> int:
        barrier.wait()
        return run(seed, path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(release_and_run, path) for path in outputs]
        codes = [future.result(timeout=WAIT_SECONDS * 2) for future in futures]

    assert codes == [0, 0]
    assert read_file(outputs[0]) == read_file(outputs[1])
    assert sorted(ids_by_name(owner_conn)) == ["funding:EUR", "funding:USD"]
