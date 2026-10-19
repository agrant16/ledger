from fastapi import APIRouter, Depends

from ledger.api.rest.dependencies import get_pool
from ledger.errors import ServiceBusyError
from ledger.infra.db import check_database

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(pool=Depends(get_pool)) -> dict[str, str] | None:  # noqa: B008
    """App readiness check."""
    check_database(pool)
    return {"status": "ok"}
