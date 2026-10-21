"""The test run never points the service at the dev database (tests/conftest.py).

`ledger_test` exists so that tests, which empty the tables they use, never touch the `ledger`
database the running service uses. Since the app factory (#100), `ledger.main.app` reads its
settings when `ledger.main` is first imported, and test modules import it while pytest collects
them, before any fixture runs. So the environment must already name the test database by then:
this file checks what `ledger.main.app` got. The first test only catches the mistake when another
module has already imported `ledger.main` during collection, as the full run does; run alone, the
import happens inside the test, after the environment is set.

No database is needed; only the connection strings are read. No correctness rule is involved.
"""

from urllib.parse import urlsplit

from ledger.infra.config import load_settings
from tests.db_helpers import TEST_DATABASE_NAME


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def test_the_real_app_built_at_import_uses_the_test_database() -> None:
    # Late on purpose: run alone, this import builds the app (see the module docstring).
    from ledger.main import app

    assert database_name(app.state.settings.app_url) == TEST_DATABASE_NAME


def test_settings_loaded_by_any_code_use_the_test_database() -> None:
    settings = load_settings()

    assert database_name(settings.app_url) == TEST_DATABASE_NAME
    assert database_name(settings.owner_url) == TEST_DATABASE_NAME
