from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local defaults match compose.yaml (dev-only passwords, Postgres published on 5432).
DEFAULT_OWNER_URL = "postgresql://ledger_owner:owner-dev-only@127.0.0.1:5432/ledger"
DEFAULT_APP_URL = "postgresql://ledger_app:app-dev-only@127.0.0.1:5432/ledger"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, case_sensitive=True)

    # The owner role runs migrations (and test fixtures). The service never connects as it.
    owner_url: str = Field(default=DEFAULT_OWNER_URL, alias="DATABASE_OWNER_URL")
    # The role the service connects as: owns no tables, explicit per-table grants only.
    app_url: str = Field(default=DEFAULT_APP_URL, alias="DATABASE_APP_URL")
    migrations_dir: Path = Field(default=DEFAULT_MIGRATIONS_DIR, alias="MIGRATIONS_DIR")
    # Fixed pool size and how long a request waits for a connection before a 503.
    pool_size: int = Field(default=10, alias="POOL_SIZE", gt=0)
    pool_timeout_seconds: float = Field(
        default=2, alias="POOL_TIMEOUT_SECONDS", gt=0, allow_inf_nan=False
    )
    # Max concurrent handler threads (anyio’s default is 40)
    handler_thread_limit: int = Field(default=20, alias="HANDLER_THREAD_LIMIT", gt=0)


def load_settings() -> Settings:
    return Settings()
