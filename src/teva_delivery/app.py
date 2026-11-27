"""Builds the FastAPI application. Uvicorn starts it with ``teva_delivery.app:create_app``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import __version__
from .api import health
from .api.errors import register_error_handlers
from .api.middleware import RequestContextMiddleware
from .api.v1.router import build_router
from .core.clock import Clock, utc_now
from .core.config import Settings
from .core.logging import configure_logging
from .persistence.database import Database
from .providers.factory import build_providers, close_providers
from .providers.workspace_auth import auth_client
from .services.container import Services, build_services
from .services.context import Context, Providers


def create_app(
    settings: Settings | None = None,
    *,
    providers: Providers | None = None,
    database: Database | None = None,
    clock: Clock = utc_now,
    auth_transport: httpx.AsyncBaseTransport | None = None,
    start_worker: bool | None = None,
) -> FastAPI:
    """Wire settings, the database, providers and services into the app.

    Tests pass their own providers, a database, a manual clock and a mock transport for the
    workspace auth service, and usually run the worker by hand instead of in its thread.
    """
    config = settings or Settings()
    configure_logging(config.log_level, config.log_format)
    db = database or Database(config.database_url)
    context = Context(settings=config, db=db, providers=Providers(), clock=clock)
    own_providers = providers is None
    context.providers = providers or build_providers(config, clock, context.config)
    services = build_services(context)
    run_worker = config.worker_enabled if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.migrate()
        if run_worker:
            services.worker.start()
        try:
            if config.auth_url is None:
                yield
                return
            async with auth_client(
                str(config.auth_url), config.auth_service_key.get_secret_value(), auth_transport
            ) as client:
                app.state.auth_client = client
                yield
        finally:
            services.worker.stop()
            if own_providers:
                close_providers(context.providers)
            if database is None:
                db.dispose()

    app = FastAPI(
        title="Teva Delivery",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.services = services
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(build_router())
    return app


def services_of(app: FastAPI) -> Services:
    services: Services = app.state.services
    return services
