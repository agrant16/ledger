"""The guard in tests/unit/conftest.py keeps unit tests away from the database.

Three parts: the function that finds offenders, the runtime block on connections, and, with
pytest's own pytester, that the hook and the fixture are really wired into a pytest run. Calling
the function directly would still pass if the hook name were misspelled and never called.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from tests.db_guard import find_database_users

UNIT_DIR = Path(__file__).resolve().parent
INTEGRATION_DIR = UNIT_DIR.parent / "integration"
GUARD_CONFTEST = UNIT_DIR / "conftest.py"
# The guard refuses before any connection is tried. If it ever stops doing so, the short timeout
# makes the test fail in seconds and not after psycopg's default 130.
UNREACHABLE = "postgresql://nobody@127.0.0.1:1/none?connect_timeout=2"


def fake_item(directory: Path, name: str, fixtures: tuple[str, ...]) -> SimpleNamespace:
    """Stands in for a collected test; the guard reads only path, nodeid and fixturenames."""
    path = directory / name
    return SimpleNamespace(
        path=path, nodeid=f"{path.parent.name}/{name}::test", fixturenames=fixtures
    )


# --- finding offenders ----------------------------------------------------------------------


def test_unit_tests_without_database_fixtures_pass() -> None:
    items = [fake_item(UNIT_DIR, "test_a.py", ("tmp_path", "monkeypatch"))]
    assert find_database_users(items, UNIT_DIR) == []


@pytest.mark.parametrize("fixture", ["migrated_database", "owner_conn", "app_pool"])
def test_a_unit_test_using_a_database_fixture_is_found(fixture: str) -> None:
    items = [fake_item(UNIT_DIR, "test_a.py", ("tmp_path", fixture))]
    (offender,) = find_database_users(items, UNIT_DIR)
    assert fixture in offender


def test_a_fixture_built_on_a_database_fixture_is_found() -> None:
    # pytest lists the whole dependency closure, so app_conn arrives together with app_pool.
    items = [fake_item(UNIT_DIR, "test_a.py", ("app_conn", "app_pool", "migrated_database"))]
    assert len(find_database_users(items, UNIT_DIR)) == 1


def test_every_offender_is_listed() -> None:
    items = [
        fake_item(UNIT_DIR, "test_a.py", ("owner_conn",)),
        fake_item(UNIT_DIR, "test_b.py", ("app_pool",)),
    ]
    found = " ".join(find_database_users(items, UNIT_DIR))
    assert "test_a.py" in found
    assert "test_b.py" in found


def test_tests_outside_tests_unit_are_ignored() -> None:
    items = [fake_item(INTEGRATION_DIR, "test_c.py", ("owner_conn", "app_pool"))]
    assert find_database_users(items, UNIT_DIR) == []


# --- connections are refused while a unit test runs -----------------------------------------


def test_psycopg_connect_is_refused() -> None:
    with pytest.raises(AssertionError, match="must not connect to the database"):
        psycopg.connect(UNREACHABLE)


def test_the_connection_class_is_refused_too() -> None:
    with pytest.raises(AssertionError, match="must not connect to the database"):
        psycopg.Connection.connect(UNREACHABLE)


def test_the_async_connection_class_is_refused_too() -> None:
    with pytest.raises(AssertionError, match="must not connect to the database"):
        asyncio.run(psycopg.AsyncConnection.connect(UNREACHABLE))


# --- the guard is wired into a real pytest run ----------------------------------------------

# Stand-ins for the real database fixtures, so a nested run needs no database.
STUB_FIXTURES = """
import pytest


@pytest.fixture
def owner_conn():
    yield None
"""


def nested_run(pytester: pytest.Pytester, test_source: str) -> pytest.RunResult:
    """Run pytest on a temporary folder that has the real guard conftest as its own, so the folder
    counts as a unit test directory."""
    pytester.makeconftest(GUARD_CONFTEST.read_text(encoding="utf-8") + STUB_FIXTURES)
    pytester.makepyfile(test_nested=test_source)
    return pytester.runpytest_inprocess()


def test_a_run_with_a_unit_test_that_uses_a_database_fixture_is_stopped(
    pytester: pytest.Pytester,
) -> None:
    result = nested_run(pytester, "def test_x(owner_conn):\n    pass\n")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*must not use the database*", "*test_nested.py::test_x*owner_conn*"]
    )


def test_a_run_with_only_clean_unit_tests_passes(pytester: pytest.Pytester) -> None:
    result = nested_run(pytester, "def test_x():\n    pass\n")
    result.assert_outcomes(passed=1)


def test_a_unit_test_that_connects_fails_inside_a_run(pytester: pytest.Pytester) -> None:
    source = f"import psycopg\n\n\ndef test_x():\n    psycopg.connect({UNREACHABLE!r})\n"
    result = nested_run(pytester, source)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*must not connect to the database*"])
