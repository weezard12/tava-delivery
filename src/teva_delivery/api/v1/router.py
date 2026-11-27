"""Version 1 of the API, under ``/api/v1``."""

from fastapi import APIRouter

from . import events, office, packing


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    router.include_router(packing.router)
    router.include_router(office.router)
    router.include_router(events.router)
    return router
