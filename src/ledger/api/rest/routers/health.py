from fastapi import APIRouter, Depends

from ledger.api.rest.dependencies import get_pool
from ledger.infra.db import check_database

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(pool=Depends(get_pool)) -> dict[str, str] | None:  # noqa: B008
    """Checks that the service can reach the database (readiness, not liveness).

    The route is left out of OpenAPI.

    Args:
        pool: The connection pool, injected.

    Returns:
        `{"status": "ok"}`.

    Raises:
        ServiceBusyError: No connection became free within the pool timeout (a 503).

    Propagates any other database error (a logged 500).
    """
    check_database(pool)
    return {"status": "ok"}
