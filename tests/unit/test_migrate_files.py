"""Finding and ordering migration files (no database). Applying them is tested in
integration/db/test_migrate_runner.py."""

from pathlib import Path

import pytest

from ledger.migrate import find_migrations


def write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


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
