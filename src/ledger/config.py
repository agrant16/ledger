import os
from dataclasses import dataclass
from pathlib import Path

# Local defaults match compose.yaml (dev-only passwords, Postgres published on 5432).
DEFAULT_OWNER_URL = "postgresql://ledger_owner:owner-dev-only@127.0.0.1:5432/ledger"
DEFAULT_APP_URL = "postgresql://ledger_app:app-dev-only@127.0.0.1:5432/ledger"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass(frozen=True)
class Settings:
    # The owner role runs migrations (and test fixtures). The service never connects as it.
    owner_url: str
    # The role the service connects as: owns no tables, explicit per-table grants only.
    app_url: str
    migrations_dir: Path
    # Fixed pool size and how long a request waits for a connection before a 503.
    pool_size: int
    pool_timeout_seconds: float


def load_settings() -> Settings:
    return Settings(
        owner_url=os.environ.get("DATABASE_OWNER_URL", DEFAULT_OWNER_URL),
        app_url=os.environ.get("DATABASE_APP_URL", DEFAULT_APP_URL),
        migrations_dir=Path(os.environ.get("MIGRATIONS_DIR", DEFAULT_MIGRATIONS_DIR)),
        pool_size=int(os.environ.get("POOL_SIZE", "10")),
        pool_timeout_seconds=float(os.environ.get("POOL_TIMEOUT_SECONDS", "2")),
    )
