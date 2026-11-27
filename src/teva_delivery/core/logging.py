"""Process-wide logging: plain text for people or JSON lines for log collectors.

Every record carries the ID of the request it belongs to, so one packing action can be
followed through the service log and matched with the ``X-Request-ID`` response header.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

LOGGER = "teva_delivery"

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestIdFilter(logging.Filter):
    """Tag records with the current request, unless the caller passed one in ``extra``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id.get()
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        current = getattr(record, "request_id", None)
        return f"{text} request={current}" if current else text


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured ``extra={"event": {...}}`` is kept as is."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if current := getattr(record, "request_id", None):
            entry["request_id"] = current
        for key in ("event", "http"):
            if isinstance(value := getattr(record, key, None), dict):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO", log_format: Literal["text", "json"] = "text"
) -> logging.Handler:
    """Send service logs to stderr, which the container runtime collects.

    Uvicorn does not log the service's own loggers by default. Calling this again
    replaces the handler, so the last app created decides the format and level.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(RequestIdFilter())
    handler.set_name("teva_delivery")

    logger = logging.getLogger(LOGGER)
    _replace_handler(logger, handler)
    logger.setLevel(level)
    logger.propagate = False

    # httpx logs every request URL at INFO, and Konimbo's token travels in the URL.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    uvicorn = logging.getLogger("uvicorn")
    if log_format == "json":
        # Uvicorn's own startup and error lines should be JSON too.
        for existing in list(uvicorn.handlers):
            uvicorn.removeHandler(existing)
        uvicorn.addHandler(handler)
    else:
        _replace_handler(uvicorn, None)
    return handler


def _replace_handler(logger: logging.Logger, handler: logging.Handler | None) -> None:
    for existing in list(logger.handlers):
        if existing.get_name() == "teva_delivery":
            logger.removeHandler(existing)
    if handler is not None:
        logger.addHandler(handler)
