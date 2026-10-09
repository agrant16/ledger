"""Applies numbered plain-SQL migrations, in filename order, as the owner role.

Run with: PYTHONPATH=src uv run python -m ledger.migrate
"""

import re
import sys
from pathlib import Path

import psycopg

from ledger.config import load_settings

MIGRATION_NAME = re.compile(r"^\d+_[a-z0-9_]+\.sql$")
# Arbitrary constant; serializes concurrent runners (e.g. two containers starting at once).
ADVISORY_LOCK_KEY = 7_262_001


def find_migrations(directory: Path) -> list[Path]:
    """Return migration files in numeric-prefix order. Fails on a bad name or repeated number."""
    if not directory.is_dir():
        return []
    files = list(directory.glob("*.sql"))
    for f in files:
        if not MIGRATION_NAME.match(f.name):
            raise ValueError(f"migration file name must look like 001_create_x.sql: {f.name}")
    seen: dict[int, str] = {}
    for f in sorted(files):
        number = int(f.name.split("_", 1)[0])
        if number in seen:
            raise ValueError(f"duplicate migration number {number}: {seen[number]} and {f.name}")
        seen[number] = f.name
    # Sort by the integer prefix, so 2_x runs before 10_x.
    return sorted(files, key=lambda f: int(f.name.split("_", 1)[0]))


def run_migrations(owner_url: str, directory: Path) -> list[str]:
    """Apply every unapplied migration, each in its own transaction. Returns names applied."""
    applied_now: list[str] = []
    files = find_migrations(directory)
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied_now.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied_now


def main() -> int:
    settings = load_settings()
    applied = run_migrations(settings.owner_url, settings.migrations_dir)
    for name in applied:
        print(f"applied {name}")
    if not applied:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
