import psycopg_pool
from fastapi import APIRouter, Depends

from ledger.api.rest.dependencies import get_pool
from ledger.infra.db import check_database

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(pool: psycopg_pool.ConnectionPool = Depends(get_pool)) -> dict[str, str] | None:  # noqa: B008
    """Liveness plus a database round trip, to confirm the app can reach Postgres."""
    check_database(pool)
    return {"status": "ok"}
