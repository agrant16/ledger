"""with_connect_timeout (tests/db_helpers.py): how the tests make a stopped database fail fast."""

from tests.db_helpers import with_connect_timeout


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
