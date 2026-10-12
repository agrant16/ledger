from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ledger.api.errors import install_error_handling
from ledger.config import load_settings
from ledger.db import create_pool
from ledger.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = create_pool(load_settings())
    pool.open(wait=True)
    app.state.pool = pool
    configure_logging()
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Ledger Service", lifespan=lifespan)

install_error_handling(app)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Liveness plus a database round trip, to confirm the app can reach Postgres."""
    with app.state.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
