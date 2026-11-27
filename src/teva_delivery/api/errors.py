"""Turns errors into the API's ``{"code": code, "detail": text}`` responses.

``code`` is the stable code the frontend switches on, as in teva-ai; ``detail`` is the Hebrew
text the employee sees, so the workspace's shared HTTP client shows it as it is. Anything
unexpected is a bug: it is logged with the request ID and answered with a generic 500, never
with internal details.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..domain.errors import ServiceError, message_for

logger = logging.getLogger("teva_delivery.errors")


def problem(code: str, status: int, message: str | None = None, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"code": code, "detail": message or message_for(code)},
        status_code=status,
        headers=headers or None,
    )


async def service_error(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ServiceError):  # pragma: no cover - registered for ServiceError
        raise error
    return problem(error.code, error.status, error.message, **error.headers)


async def invalid_request(request: Request, error: Exception) -> JSONResponse:
    # The field names help a developer; values are never echoed back.
    fields = []
    if isinstance(error, RequestValidationError):
        fields = [".".join(str(part) for part in item.get("loc", ())) for item in error.errors()]
    response = problem("request_invalid", 422)
    response.headers["X-Invalid-Fields"] = ",".join(fields)[:200]
    return response


async def http_error(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, HTTPException):  # pragma: no cover - registered for HTTPException
        raise error
    if error.status_code == 404:
        return problem("not_found", 404)
    if error.status_code == 405:
        return problem("action_unknown", 405)
    return problem("request_invalid", error.status_code)


async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
    current = request.scope.get("state", {}).get("request_id")
    # This runs after the request middleware has finished, so pass the ID explicitly.
    logger.error(
        "Unhandled error on %s %s",
        request.method,
        request.url.path,
        exc_info=error,
        extra={"request_id": current},
    )
    response = problem("internal_error", 500)
    if current:
        response.headers["X-Request-ID"] = current
    return response


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ServiceError, service_error)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(Exception, unexpected_error)
