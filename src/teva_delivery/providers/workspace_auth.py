"""Checks workspace sessions with the shared Teva auth service, as teva-printer does.

The browser's session cookie, its Origin and its CSRF token go to the auth service, which
answers with the approved account or refuses. The service authenticates itself with its own
key (TEVA_AUTH_DELIVERY_KEY on the auth side).
"""

import asyncio
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..domain.errors import ServiceError
from ..domain.identity import Principal

AUTH_TIMEOUT = 3.0
MAX_AUTH_RESPONSE_BYTES = 4096
VERIFY_PATH = "/internal/session/verify"


class ApprovedAccount(BaseModel):
    id: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    role: Literal["admin", "user"]


def plausible_session_token(token: str | None) -> bool:
    """Reject cookies the auth service could never have issued without asking it."""
    return token is not None and 20 <= len(token) <= 256 and token.isascii()


async def verify_session(
    client: httpx.AsyncClient,
    token: str | None,
    *,
    method: str,
    origin: str | None,
    csrf_token: str | None,
) -> Principal:
    """Return the approved account behind a session cookie.

    Raises ``invalid_session`` (401) and ``forbidden`` (403) as the auth service decides,
    and ``workspace_auth_unavailable`` (503) when it cannot be asked or answers oddly.
    """
    if not plausible_session_token(token):
        raise ServiceError("invalid_session")
    try:
        async with (
            asyncio.timeout(AUTH_TIMEOUT),
            client.stream(
                "POST",
                VERIFY_PATH,
                json={
                    "token": token,
                    "method": method,
                    "origin": origin,
                    "csrf_token": csrf_token,
                },
            ) as response,
        ):
            if response.status_code == 401:
                raise ServiceError("invalid_session")
            if response.status_code == 403:
                raise ServiceError("forbidden")
            if response.status_code != 200:
                raise ServiceError("workspace_auth_unavailable")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=MAX_AUTH_RESPONSE_BYTES):
                if len(body) + len(chunk) > MAX_AUTH_RESPONSE_BYTES:
                    raise ServiceError("workspace_auth_unavailable")
                body.extend(chunk)
            account = ApprovedAccount.model_validate_json(body)
    except httpx.HTTPError, TimeoutError, ValidationError:
        raise ServiceError("workspace_auth_unavailable") from None
    return Principal(account.id, account.role)


def auth_client(
    base_url: str, service_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {service_key}"},
        timeout=AUTH_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )
