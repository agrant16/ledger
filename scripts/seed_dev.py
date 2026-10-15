"""Seeds the local development database with accounts for manual API testing.

Run with: PYTHONPATH=src uv run python scripts/seed_dev.py

Creates one funding account (allow_negative) and five protected accounts for each of USD and EUR,
through the service's create_account, as the app role. Every balance starts at 0: there are no
transfers or entries yet. An account whose name already exists is skipped, so the script can run
again on a database that holds data you created through the API. After scripts/reset_db.py the ids
are always the same (funding:USD 1, funding:EUR 2, then the USD customers 3 to 7 and the EUR
customers 8 to 12).

It connects as the app role from DATABASE_APP_URL and refuses any host that is not this machine.
"""

import sys
from urllib.parse import urlsplit

import psycopg

from ledger.infra.config import load_settings
from ledger.service.account_service import create_account

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# (account_name, currency, allow_negative), in creation order.
SEED_ACCOUNTS: list[tuple[str, str, bool]] = [
    ("funding:USD", "USD", True),
    ("funding:EUR", "EUR", True),
    *((f"customer:{name}", "USD", False) for name in ("alice", "bob", "carol", "dave", "erin")),
    *((f"customer:{name}", "EUR", False) for name in ("frank", "grace", "heidi", "ivan", "judy")),
]


def check_local(app_url: str) -> str:
    """Return the database name, or exit if the URL does not point at this machine."""
    parts = urlsplit(app_url)
    if parts.hostname not in LOCAL_HOSTS:
        sys.exit(f"Refusing to seed a database on {parts.hostname!r}: only local databases.")
    return parts.path.lstrip("/")


def seed(app_url: str) -> tuple[list[str], list[str]]:
    """Create the missing seed accounts in one database transaction. Returns the names created and
    the names skipped because they already existed."""
    created: list[str] = []
    skipped: list[str] = []
    with psycopg.connect(app_url) as conn:
        existing = {row[0] for row in conn.execute("SELECT account_name FROM accounts")}
        for account_name, currency, allow_negative in SEED_ACCOUNTS:
            if account_name in existing:
                skipped.append(account_name)
                continue
            account = create_account(conn, account_name, currency, allow_negative)
            created.append(f"{account.id} {account.account_name} {account.currency}")
    return created, skipped


def main() -> int:
    app_url = load_settings().app_url
    database = check_local(app_url)
    created, skipped = seed(app_url)
    for line in created:
        print(f"created account {line}")
    for name in skipped:
        print(f"skipped {name} (already exists)")
    print(f"database {database!r} seeded: {len(created)} created, {len(skipped)} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
