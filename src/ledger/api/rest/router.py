from fastapi import APIRouter

from ledger.api.rest.routers.accounts import router as accounts_router
from ledger.api.rest.routers.health import router as health_router
from ledger.api.rest.routers.transfers import router as transfers_router

router = APIRouter()

router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])

router.include_router(health_router, tags=["health"])

router.include_router(transfers_router, prefix="/transfers", tags=["transfers"])
