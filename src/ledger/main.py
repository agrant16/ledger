from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.router import router as api_router
from ledger.infra.config import load_settings
from ledger.infra.db import create_pool
from ledger.infra.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = load_settings()
    pool = create_pool(settings)
    pool.open(wait=True)
    app.state.pool = pool
    to_thread.current_default_thread_limiter().total_tokens = settings.handler_thread_limit
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Ledger Service", lifespan=lifespan)

install_error_handling(app)

app.include_router(api_router)
