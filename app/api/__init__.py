"""API router for the rebuilt AI Business Studio."""

from fastapi import APIRouter

from . import businesses, capabilities


api_router = APIRouter(prefix="/api")
api_router.include_router(businesses.router)
api_router.include_router(capabilities.router)
