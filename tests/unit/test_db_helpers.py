"""The URL helpers in tests/db_helpers.py: how the tests make a stopped database fail fast, and how
they are kept off the service's own database."""

from urllib.parse import urlsplit

from tests.db_helpers import TEST_DATABASE_NAME, with_connect_timeout, with_database


def test_a_url_with_no_query_gets_one() -> None:
    url = "postgresql://owner:pw@127.0.0.1:5432/ledger"
    assert with_connect_timeout(url) == url + "?connect_timeout=5"


def test_a_url_with_a_query_gets_the_timeout_appended() -> None:
    url = "postgresql://owner:pw@127.0.0.1:5432/ledger?sslmode=disable"
    assert with_connect_timeout(url) == url + "&connect_timeout=5"


def test_a_timeout_already_in_the_url_is_left_alone() -> None:
    url = "postgresql://owner:pw@127.0.0.1:5432/ledger?connect_timeout=30"
    assert with_connect_timeout(url) == url


def test_the_number_of_seconds_can_be_chosen() -> None:
    assert (
        with_connect_timeout("postgresql://h/db", seconds=2)
        == "postgresql://h/db?connect_timeout=2"
    )


# --- with_database --------------------------------------------------------------------------


def test_with_database_changes_only_the_database_name() -> None:
    url = "postgresql://owner:pw@127.0.0.1:5432/ledger"

    assert with_database(url, "ledger_test") == "postgresql://owner:pw@127.0.0.1:5432/ledger_test"


def test_with_database_keeps_the_query_and_special_characters_in_the_password() -> None:
    url = "postgresql://owner:p%40ss%3Aw%2Frd@db.example:6543/ledger?sslmode=require&x=1"

    assert with_database(url, "other") == (
        "postgresql://owner:p%40ss%3Aw%2Frd@db.example:6543/other?sslmode=require&x=1"
    )


def test_with_database_adds_a_name_to_a_url_that_has_none() -> None:
    assert with_database("postgresql://owner:pw@127.0.0.1:5432", "ledger_test") == (
        "postgresql://owner:pw@127.0.0.1:5432/ledger_test"
    )


# --- the test run never uses the service's database -----------------------------------------


def test_load_settings_points_at_the_test_database_during_a_test_run() -> None:
    """Code that reads its own settings (the app's startup, the property test) must also land in
    the test database, because the tests empty the tables they use."""
    from ledger.config import load_settings

    configured = load_settings()

    assert urlsplit(configured.owner_url).path == f"/{TEST_DATABASE_NAME}"
    assert urlsplit(configured.app_url).path == f"/{TEST_DATABASE_NAME}"
    assert TEST_DATABASE_NAME != "ledger"
