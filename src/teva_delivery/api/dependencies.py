"""Shared request dependencies: the services, the signed-in employee and print credentials."""

import re
from typing import Annotated

import httpx
from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from ..domain.errors import ServiceError
from ..domain.identity import Principal
from ..domain.ports import PrintCredentials
from ..providers.workspace_auth import verify_session
from ..services.container import Services

DEMO_USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")


def get_services(connection: HTTPConnection) -> Services:
    services: Services = connection.app.state.services
    return services


type ServicesDep = Annotated[Services, Depends(get_services)]


async def authenticate(connection: HTTPConnection, method: str) -> Principal:
    """The employee behind a request or WebSocket, as the workspace auth service says.

    Demo mode with sign-in turned off accepts everyone as an admin, named by the optional
    X-Demo-User header, so browser tests can act as two different employees.
    """
    settings = get_services(connection).context.settings
    if settings.auth_disabled:
        name = connection.headers.get("x-demo-user") or connection.query_params.get("demo_user")
        if name and DEMO_USER.fullmatch(name):
            return Principal(name, "admin")
        return Principal("demo-user", "admin")
    client: httpx.AsyncClient | None = getattr(connection.app.state, "auth_client", None)
    if client is None:
        raise ServiceError("workspace_auth_unavailable")
    return await verify_session(
        client,
        connection.cookies.get(settings.auth_cookie_name),
        method=method,
        origin=connection.headers.get("origin"),
        csrf_token=connection.headers.get("x-csrf-token"),
    )


async def current_principal(request: Request) -> Principal:
    return await authenticate(request, request.method)


type Actor = Annotated[Principal, Depends(current_principal)]


async def current_admin(actor: Actor) -> Principal:
    if not actor.admin:
        raise ServiceError("admin_required")
    return actor


type Admin = Annotated[Principal, Depends(current_admin)]


def print_credentials(request: Request) -> PrintCredentials:
    """The employee's own session, passed on to teva-printer so it records who printed."""
    settings = get_services(request).context.settings
    if settings.auth_disabled:
        return PrintCredentials()
    return PrintCredentials(
        cookie_name=settings.auth_cookie_name,
        session=request.cookies.get(settings.auth_cookie_name),
        origin=request.headers.get("origin"),
        csrf_token=request.headers.get("x-csrf-token"),
    )


type Credentials = Annotated[PrintCredentials, Depends(print_credentials)]
