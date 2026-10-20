from pathlib import Path

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local defaults match compose.yaml (dev-only passwords, Postgres published on 5432).
DEFAULT_OWNER_URL = "postgresql://ledger_owner:owner-dev-only@127.0.0.1:5432/ledger"
DEFAULT_APP_URL = "postgresql://ledger_app:app-dev-only@127.0.0.1:5432/ledger"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

PUBLIC_SETTINGS = ["pool_size", "pool_timeout_seconds", "handler_thread_limit", "concurrency_cap"]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, case_sensitive=True)

    # The owner role runs migrations (and test fixtures). The service never connects as it.
    owner_url: str = Field(default=DEFAULT_OWNER_URL, alias="DATABASE_OWNER_URL")
    # The role the service connects as: owns no tables, explicit per-table grants only.
    app_url: str = Field(default=DEFAULT_APP_URL, alias="DATABASE_APP_URL")
    migrations_dir: Path = Field(default=DEFAULT_MIGRATIONS_DIR, alias="MIGRATIONS_DIR")
    # Fixed pool size and how long a request waits for a connection before a 503.
    pool_size: PositiveInt = Field(default=10, alias="POOL_SIZE")
    pool_timeout_seconds: float = Field(
        default=2.0, alias="POOL_TIMEOUT_SECONDS", gt=0, allow_inf_nan=False
    )
    # Max concurrent handler threads (anyio’s default is 40)
    handler_thread_limit: PositiveInt = Field(default=20, alias="HANDLER_THREAD_LIMIT")
    # Max concurrent HTTP requests before a 503 is returned
    concurrency_cap: PositiveInt = Field(default=40, alias="CONCURRENCY_CAP")

    def startup_log_message(self):
        """ Returns a startup log message that lists public settings key/value pairs.
        Ignores database urls and migration directories. If any non-public settings
        are added going forward they will need to be added to the list.
        """
        message = " ".join(
            "{k}={v}".format(k=k, v=v)
            for k, v in self.model_dump().items()
            if k in PUBLIC_SETTINGS
        )
        return f"Starting app: {message}"

def load_settings() -> Settings:
    return Settings()
