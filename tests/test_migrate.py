import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from ledger.migrate import find_migrations, run_migrations


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


def test_concurrent_runners_apply_each_migration_exactly_once(
    scratch_url: str, tmp_path: Path
) -> None:
    # The sleep keeps the first runner inside its migration long enough that the second
    # would, without the advisory lock, read an empty schema_migrations and apply it too.
    write(
        tmp_path,
        "001_slow.sql",
        "CREATE TABLE t (x int); INSERT INTO t VALUES (1); SELECT pg_sleep(1);",
    )
    start = threading.Barrier(2)

    def runner() -> list[str]:
        start.wait()
        return run_migrations(scratch_url, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(runner), pool.submit(runner)]]

    assert sorted(results) == [[], ["001_slow.sql"]]
    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_dollar_quoted_blocks_are_not_split_at_their_semicolons(
    scratch_url: str, tmp_path: Path
) -> None:
    """A DO block holds several statements and a '%' in a RAISE message, and sits in the same file
    as a CREATE TABLE. The runner sends the whole file as one string and Postgres does the parsing,
    so the block must run as one statement and the table must still be created after it."""
    write(
        tmp_path,
        "001_block_then_table.sql",
        "DO $$ BEGIN PERFORM 1; PERFORM 2; RAISE NOTICE 'value %', 42; END $$;\n"
        "CREATE TABLE t (x int);",
    )
    assert run_migrations(scratch_url, tmp_path) == ["001_block_then_table.sql"]
    with psycopg.connect(scratch_url) as conn:
        assert conn.execute("SELECT to_regclass('t') IS NOT NULL").fetchone() == (True,)


def test_missing_directory_means_no_migrations(tmp_path: Path) -> None:
    assert find_migrations(tmp_path / "nope") == []


def test_badly_named_file_is_rejected(tmp_path: Path) -> None:
    write(tmp_path, "create_accounts.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="migration file name"):
        find_migrations(tmp_path)
