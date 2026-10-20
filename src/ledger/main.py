import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI

from ledger.api.rest.error_handlers import install_error_handling
from ledger.api.rest.openapi import install_openapi
from ledger.api.rest.router import router as api_router
from ledger.infra.config import Settings, load_settings
from ledger.infra.db import create_pool
from ledger.infra.logging_config import configure_logging

logger: logging.Logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Ledger Service", lifespan=lifespan)
    app.state.settings = settings if settings is not None else load_settings()
    install_error_handling(app, app.state.settings.concurrency_cap)
    app.include_router(api_router)
    install_openapi(app)
    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    to_thread.current_default_thread_limiter().total_tokens = (
        app.state.settings.handler_thread_limit
    )
    logger.info(app.state.settings.get_startup_log_message())
    pool = create_pool(app.state.settings)
    pool.open(wait=True)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = create_app()
