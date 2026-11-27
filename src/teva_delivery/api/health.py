"""Health checks for Docker and the reverse proxy. They need no sign-in."""

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..domain.errors import ServiceError
from .dependencies import ServicesDep

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
def health(services: ServicesDep) -> dict[str, str]:
    """Ready to serve: the database answers."""
    try:
        with services.context.db.session() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise ServiceError("service_unavailable") from None
    return {"status": "ok"}


@router.get("/livez")
async def live() -> dict[str, str]:
    """The process is serving requests. Never touches the database."""
    return {"status": "ok"}
