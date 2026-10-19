from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.router import router as api_router
from ledger.infra.config import load_settings
from ledger.infra.db import create_pool
from ledger.infra.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    pool = create_pool(load_settings())
    pool.open(wait=True)
    app.state.pool = pool
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        load_settings().handler_thread_limit
    )
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Ledger Service", lifespan=lifespan)

install_error_handling(app)

app.include_router(api_router)
