"""The guard in tests/unit/conftest.py rejects unit tests that use the database."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.conftest import UNIT_DIR, pytest_collection_modifyitems

INTEGRATION_DIR = UNIT_DIR.parent / "integration"


def fake_item(directory: Path, name: str, fixtures: tuple[str, ...]) -> SimpleNamespace:
    """Stands in for a collected test; the guard reads only path, nodeid and fixturenames."""
    path = directory / name
    return SimpleNamespace(
        path=path, nodeid=f"{path.parent.name}/{name}::test", fixturenames=fixtures
    )


def test_unit_tests_without_database_fixtures_pass() -> None:
    items = [fake_item(UNIT_DIR, "test_a.py", ("tmp_path", "monkeypatch"))]
    pytest_collection_modifyitems(items)  # does not raise


@pytest.mark.parametrize("fixture", ["migrated_database", "owner_conn", "app_pool"])
def test_a_unit_test_using_a_database_fixture_is_rejected(fixture: str) -> None:
    items = [fake_item(UNIT_DIR, "test_a.py", ("tmp_path", fixture))]
    with pytest.raises(pytest.UsageError, match=fixture):
        pytest_collection_modifyitems(items)


def test_a_fixture_built_on_a_database_fixture_is_rejected() -> None:
    # pytest lists the whole dependency closure, so app_conn arrives together with app_pool.
    items = [fake_item(UNIT_DIR, "test_a.py", ("app_conn", "app_pool", "migrated_database"))]
    with pytest.raises(pytest.UsageError):
        pytest_collection_modifyitems(items)


def test_every_offender_is_listed() -> None:
    items = [
        fake_item(UNIT_DIR, "test_a.py", ("owner_conn",)),
        fake_item(UNIT_DIR, "test_b.py", ("app_pool",)),
    ]
    with pytest.raises(pytest.UsageError) as excinfo:
        pytest_collection_modifyitems(items)
    assert "test_a.py" in str(excinfo.value)
    assert "test_b.py" in str(excinfo.value)


def test_tests_outside_tests_unit_are_ignored() -> None:
    items = [fake_item(INTEGRATION_DIR, "test_c.py", ("owner_conn", "app_pool"))]
    pytest_collection_modifyitems(items)  # does not raise