"""Settings defaults (ledger/infra/config.py).

The default migrations directory is worked out from the location of the config module. When the
module moves, the number of parent directories to climb changes, and `find_migrations` quietly
returns an empty list for a directory that does not exist: the database would be left without a
schema and the failure would show up much later. This test makes the mistake loud.

The numeric settings (pool size, pool wait, handler thread limit, concurrency cap) are validated
when they are loaded: a bad value fails at startup with an error that names the environment
variable (#93). No correctness rule is involved; valid configurations behave as before.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ledger.infra.config import (
    DEFAULT_APP_URL,
    DEFAULT_MIGRATIONS_DIR,
    DEFAULT_OWNER_URL,
    load_settings,
)
from ledger.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_default_migrations_directory_is_the_repositorys_migrations_folder() -> None:
    assert DEFAULT_MIGRATIONS_DIR.resolve() == (REPO_ROOT / "migrations").resolve()
    assert (DEFAULT_MIGRATIONS_DIR / "001_create_accounts.sql").is_file()


def test_the_settings_use_that_directory_unless_told_otherwise(monkeypatch) -> None:
    monkeypatch.delenv("MIGRATIONS_DIR", raising=False)

    assert load_settings().migrations_dir == DEFAULT_MIGRATIONS_DIR


def test_the_migrations_directory_can_be_set_by_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))

    assert load_settings().migrations_dir == tmp_path


def test_the_handler_thread_limit_defaults_to_20(monkeypatch) -> None:
    monkeypatch.delenv("HANDLER_THREAD_LIMIT", raising=False)

    assert load_settings().handler_thread_limit == 20


def test_the_handler_thread_limit_can_be_set_by_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("HANDLER_THREAD_LIMIT", "7")

    assert load_settings().handler_thread_limit == 7


POSITIVE_INTEGER_VARIABLES = ["POOL_SIZE", "HANDLER_THREAD_LIMIT", "CONCURRENCY_CAP"]
POSITIVE_NUMBER_VARIABLES = [*POSITIVE_INTEGER_VARIABLES, "POOL_TIMEOUT_SECONDS"]


def test_every_setting_has_its_default_when_the_environment_is_empty(monkeypatch) -> None:
    for name in [
        "DATABASE_OWNER_URL",
        "DATABASE_APP_URL",
        "MIGRATIONS_DIR",
        *POSITIVE_NUMBER_VARIABLES,
    ]:
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.owner_url == DEFAULT_OWNER_URL
    assert settings.app_url == DEFAULT_APP_URL
    assert settings.migrations_dir == DEFAULT_MIGRATIONS_DIR
    assert settings.pool_size == 10
    assert settings.pool_timeout_seconds == 2
    assert settings.handler_thread_limit == 20
    assert settings.concurrency_cap == 40


def test_valid_numeric_values_are_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("POOL_SIZE", "3")
    monkeypatch.setenv("POOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("HANDLER_THREAD_LIMIT", "1")
    monkeypatch.setenv("CONCURRENCY_CAP", "5")

    settings = load_settings()

    assert settings.pool_size == 3
    assert settings.pool_timeout_seconds == 0.5
    assert settings.handler_thread_limit == 1
    assert settings.concurrency_cap == 5


@pytest.mark.parametrize("value", ["0", "-1", "-20"])
@pytest.mark.parametrize("name", POSITIVE_NUMBER_VARIABLES)
def test_a_zero_or_negative_value_is_rejected_and_names_the_variable(
    monkeypatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_settings()


@pytest.mark.parametrize("value", ["abc", "", "ten", "1e", "5 threads"])
@pytest.mark.parametrize("name", POSITIVE_NUMBER_VARIABLES)
def test_a_non_numeric_value_is_rejected_and_names_the_variable(
    monkeypatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_settings()


@pytest.mark.parametrize("name", POSITIVE_INTEGER_VARIABLES)
def test_a_decimal_is_rejected_for_a_whole_number_setting(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "2.5")

    with pytest.raises(ValueError, match=name):
        load_settings()


@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
def test_an_infinite_or_nan_pool_wait_is_rejected(monkeypatch, value: str) -> None:
    # `inf` would pass a plain "greater than 0" check and give a pool that never times out.
    monkeypatch.setenv("POOL_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="POOL_TIMEOUT_SECONDS"):
        load_settings()


def test_the_app_refuses_to_start_on_a_bad_setting_and_names_it(monkeypatch) -> None:
    def create_pool_must_not_be_reached(*args, **kwargs):
        raise RuntimeError("startup went on to create the pool with a bad setting")

    # With a limit of 0 the app would otherwise start and then hang with no thread to run a
    # handler on, so a missing check must fail here and not stall the run.
    monkeypatch.setattr("ledger.main.create_pool", create_pool_must_not_be_reached)
    monkeypatch.setenv("HANDLER_THREAD_LIMIT", "0")

    with pytest.raises(ValueError, match="HANDLER_THREAD_LIMIT"):
        with TestClient(app):
            pass
