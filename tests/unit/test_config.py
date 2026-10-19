"""Settings defaults (ledger/infra/config.py).

The default migrations directory is worked out from the location of the config module. When the
module moves, the number of parent directories to climb changes, and `find_migrations` quietly
returns an empty list for a directory that does not exist: the database would be left without a
schema and the failure would show up much later. This test makes the mistake loud.
"""

from pathlib import Path

from ledger.infra.config import DEFAULT_MIGRATIONS_DIR, load_settings

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
