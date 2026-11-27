"""Request IDs and the access log.

Each request gets an ID, taken from a well-formed ``X-Request-ID`` header (as a proxy
may set) or generated. It is returned in the response header and attached to every log
line written while the request runs. The access log records the method, path, status
and duration, never the query string.
"""

import logging
import re
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.logging import request_id

REQUEST_ID_HEADER = "x-request-id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Docker polls these every few seconds. Log them only at DEBUG.
QUIET_PATHS = frozenset({"/healthz", "/livez"})

access_log = logging.getLogger("teva_delivery.access")


def incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", ()):
        if name == REQUEST_ID_HEADER.encode():
            text: str = value.decode("latin-1")
            if _VALID_REQUEST_ID.fullmatch(text):
                return text
            break
    return uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        current = incoming_request_id(scope)
        scope.setdefault("state", {})["request_id"] = current
        token = request_id.set(current)
        started = perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", current)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            method, path = scope.get("method", ""), scope.get("path", "")
            duration_ms = round((perf_counter() - started) * 1000)
            access_log.log(
                logging.DEBUG if path in QUIET_PATHS else logging.INFO,
                "%s %s %s %dms",
                method,
                path,
                status,
                duration_ms,
                extra={
                    "http": {
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": duration_ms,
                    }
                },
            )
            request_id.reset(token)
