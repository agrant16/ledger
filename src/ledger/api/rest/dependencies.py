from typing import Annotated

from fastapi import Depends, Request

from ledger.infra.config import Settings
from ledger.infra.db import Pool


# Must stay `async def`: a plain `def` dependency runs on a worker thread from the default
# limiter, so `/health` would queue behind traffic for a handler thread (issue #155).
async def get_pool(request: Request):
    """Gets the connection pool from the application state.

    Args:
        request: The current request.

    Returns:
        The connection pool.
    """
    return request.app.state.pool


# declared here as get_pool has to be defined first
PoolDep = Annotated[Pool, Depends(get_pool)]


# `async def` for the same reason as `get_pool`.
async def get_settings(request: Request):
    """Gets the settings from the application state.

    Args:
        request: The current request.

    Returns:
        The settings configuration.
    """
    return request.app.state.settings


# declared here as get_settings has to be defined first
SettingsDep = Annotated[Settings, Depends(get_settings)]
