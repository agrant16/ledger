from pathlib import Path

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local defaults match compose.yaml (dev-only passwords, Postgres published on 5432).
DEFAULT_OWNER_URL = "postgresql://ledger_owner:owner-dev-only@127.0.0.1:5432/ledger"
DEFAULT_APP_URL = "postgresql://ledger_app:app-dev-only@127.0.0.1:5432/ledger"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

PUBLIC_SETTINGS = ["pool_size", "pool_timeout_seconds", "handler_thread_limit", "concurrency_cap"]
CONCURRENCY_CAP = 40


class Settings(BaseSettings):
    """Service configuration, read from environment variables.

    Each field is read from the environment variable named in its alias (for example
    `POOL_SIZE`). Names are case sensitive, unknown variables are ignored, and an
    instance is frozen. A field with no variable set falls back to a default that
    matches `compose.yaml`: dev-only passwords, with Postgres on 127.0.0.1:5432. A
    value that fails validation (for example a zero pool size) raises a pydantic
    `ValidationError` when `Settings()` is built, so a bad configuration stops
    startup.
    """

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
    concurrency_cap: PositiveInt = Field(default=CONCURRENCY_CAP, alias="CONCURRENCY_CAP")

    def get_startup_log_message(self) -> str:
        """Returns the startup log line, `Starting app: name=value ...`, for the public settings.

        Database URLs and migration directories are never included.

        Returns:
            The log message.
        """
        message = " ".join(f"{k}={v}" for k, v in self.model_dump().items() if k in PUBLIC_SETTINGS)
        return f"Starting app: {message}"


def load_settings() -> Settings:
    """Loads the service settings from the environment.

    Returns:
        The settings, with defaults for any variable that is not set.

    Raises:
        pydantic.ValidationError: A variable fails validation, such as a zero pool size.
    """
    return Settings()
