from fastapi import Request


def get_pool(request: Request):
    """Gets the connection pool from the application state.

    Args:
        request: The current request.

    Returns:
        The connection pool.
    """
    return request.app.state.pool


def get_settings(request: Request):
    """Gets the settings from the application state.

    Args:
        request: The current request.

    Returns:
        The settings configuration.
    """
    return request.app.state.settings
