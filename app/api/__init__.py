"""API router for the rebuilt AI Business Studio."""

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_account

from . import auth, businesses, capabilities


api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(businesses.router, dependencies=[Depends(require_account)])
api_router.include_router(capabilities.router, dependencies=[Depends(require_account)])
