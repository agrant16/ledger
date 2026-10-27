from fastapi import Request


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


# `async def` for the same reason as `get_pool`.
async def get_settings(request: Request):
    """Gets the settings from the application state.

    Args:
        request: The current request.

    Returns:
        The settings configuration.
    """
    return request.app.state.settings
