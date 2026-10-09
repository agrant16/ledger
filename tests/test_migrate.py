import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ledger.config import Settings
from ledger.migrate import find_migrations, run_migrations


@pytest.fixture
def scratch_url(settings: Settings, owner_conn: psycopg.Connection) -> Iterator[str]:
    """Owner-role URL whose search_path is a throwaway schema, so the runner's bookkeeping
    and the test migrations never touch the real tables."""
    schema = f"mig_test_{uuid.uuid4().hex[:8]}"
    owner_conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in settings.owner_url else "?"
    yield f"{settings.owner_url}{sep}options=-csearch_path%3D{schema}"
    owner_conn.execute(f"DROP SCHEMA {schema} CASCADE")


def write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_applies_in_order_once(scratch_url: str, tmp_path: Path) -> None:
    write(tmp_path, "002_second.sql", "INSERT INTO t VALUES (2);")
    write(tmp_path, "001_first.sql", "CREATE TABLE t (x int);")

    assert run_migrations(scratch_url, tmp_path) == ["001_first.sql", "002_second.sql"]
    assert run_migrations(scratch_url, tmp_path) == []

    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(2,)]


def test_failed_migration_rolls_back_and_is_retried(scratch_url: str, tmp_path: Path) -> None:
    write(tmp_path, "001_bad.sql", "CREATE TABLE t (x int); SELECT 1/0;")

    with pytest.raises(psycopg.errors.DivisionByZero):
        run_migrations(scratch_url, tmp_path)

    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT to_regclass('t')").fetchone() == (None,)
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (0,)

    write(tmp_path, "001_bad.sql", "CREATE TABLE t (x int);")
    assert run_migrations(scratch_url, tmp_path) == ["001_bad.sql"]


def test_sorts_by_numeric_prefix_not_lexically(tmp_path: Path) -> None:
    for name in ("10_c.sql", "2_b.sql", "001_a.sql"):
        write(tmp_path, name, "SELECT 1;")
    assert [f.name for f in find_migrations(tmp_path)] == ["001_a.sql", "2_b.sql", "10_c.sql"]


def test_duplicate_numbers_are_rejected_even_if_padded_differently(tmp_path: Path) -> None:
    write(tmp_path, "002_create_accounts.sql", "SELECT 1;")
    write(tmp_path, "2_add_index.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="duplicate migration number 2"):
        find_migrations(tmp_path)


def test_missing_directory_means_no_migrations(tmp_path: Path) -> None:
    assert find_migrations(tmp_path / "nope") == []


def test_badly_named_file_is_rejected(tmp_path: Path) -> None:
    write(tmp_path, "create_accounts.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="migration file name"):
        find_migrations(tmp_path)
