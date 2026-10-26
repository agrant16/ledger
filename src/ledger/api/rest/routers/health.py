from fastapi import APIRouter, Depends
from starlette.requests import Request

from ledger.api.rest.dependencies import get_pool
from ledger.errors import NotReadyError
from ledger.infra.readiness import NOT_READY_DETAIL, check_readiness, pool_state

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(request: Request, pool=Depends(get_pool)) -> dict[str, str]:  # noqa: B008
    """Reports whether the service is ready to take traffic. (ADR 0004)

    A readiness check, not a liveness check. A pool with every connection in use does not make
    the service unready; it is reported in the `pool` field. The route is left out of OpenAPI.

    Args:
        request: The incoming request.
        pool: The request connection pool, injected.

    Returns:
        `{"status": "ok", "pool": "ok"}`, or `"pool": "saturated"` when every pool connection
        is in use.

    Raises:
        NotReadyError: The database cannot be reached or queried, it is read-only, or a required
            table or privilege is missing.
    """
    settings = request.app.state.settings
    result = check_readiness(settings.app_url, settings.health_timeout_seconds)
    if not result.ready:
        raise NotReadyError(NOT_READY_DETAIL[result.failed_check]) from result.cause
    return {"status": "ok", "pool": pool_state(pool)}
