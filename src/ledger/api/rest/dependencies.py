from fastapi import Request


async def get_pool(request: Request):
    """Extracts the pool from app state automatically."""
    return request.app.state.pool
