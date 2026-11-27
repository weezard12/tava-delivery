"""Settings validation, logging and building the providers the settings ask for."""

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from teva_delivery.core.clock import ManualClock
from teva_delivery.core.config import Settings
from teva_delivery.core.logging import configure_logging, request_id
from teva_delivery.providers.cargo import CargoClient
from teva_delivery.providers.factory import build_providers, close_providers
from teva_delivery.providers.interpreter import ClaudeInterpreter
from teva_delivery.providers.konimbo import KonimboClient
from teva_delivery.providers.printer import PrinterClient
from teva_delivery.providers.speech import SpeechClient
from teva_delivery.services.warehouse_config import WarehouseConfig

LIVE: dict[str, Any] = {
    "mode": "live",
    "auth_url": "https://auth.internal",
    "auth_service_key": "k" * 40,
    "postgres_host": "db",
    "postgres_database": "delivery",
    "postgres_user": "delivery",
    "postgres_password": "secret-password",
}


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]  # pydantic-settings option


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "live"},
        {**LIVE, "postgres_password": ""},
        {**LIVE, "auth_disabled": True},
        {"auth_service_key": "k" * 40},
        {"auth_url": "https://auth.internal/path", "auth_service_key": "k" * 40},
        {"auth_url": "https://auth.internal", "auth_service_key": "short"},
        {"timezone": "Mars/Olympus"},
        {"sync_interval_seconds": 5},
        {"ai_provider": "anthropic"},
    ],
)
def test_invalid_settings_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        settings(**values)


def test_secrets_never_appear_in_errors_or_reprs() -> None:
    live = settings(**LIVE, konimbo_token="konimbo-secret")
    assert "konimbo-secret" not in repr(live)
    assert "secret-password" not in repr(live)
    with pytest.raises(ValidationError) as error:
        settings(**{**LIVE, "auth_service_key": "short-secret"})
    assert "short-secret" not in str(error.value)


def test_blank_values_and_rules_from_the_environment() -> None:
    parsed = settings(
        printer_url="",
        shipping_rules='[{"match": "איסוף", "profile": "none"}, {"profile": "express"}]',
        log_format="JSON",
        log_level="debug",
    )
    assert parsed.printer_url is None
    assert len(parsed.shipping_rules) == 2
    assert (parsed.log_format, parsed.log_level) == ("json", "DEBUG")


def test_status_names_must_not_overlap() -> None:
    base = WarehouseConfig.from_settings(settings())
    with pytest.raises(ValidationError):
        WarehouseConfig.model_validate(
            {**base.model_dump(), "status_urgent": base.status_regular[0]}
        )
    with pytest.raises(ValidationError):
        WarehouseConfig.model_validate({**base.model_dump(), "shipment_number_pattern": r"\d+"})


def test_live_providers_are_built_only_when_configured(tmp_path: Path) -> None:
    clock = ManualClock()
    bare = build_providers(settings(**LIVE, data_dir=tmp_path), clock, lambda: None)  # type: ignore[arg-type, return-value]
    assert (bare.store, bare.carrier, bare.printer, bare.transcriber, bare.interpreter) == (
        None,
        None,
        None,
        None,
        None,
    )
    full = build_providers(
        settings(
            **LIVE,
            konimbo_token="t",
            cargo_token="c",
            printer_url="http://printer:8000",
            stt_url="http://stt:9000",
            ai_provider="anthropic",
            anthropic_api_key="a",
        ),
        clock,
        lambda: None,  # type: ignore[arg-type, return-value]
    )
    assert isinstance(full.store, KonimboClient)
    assert isinstance(full.carrier, CargoClient)
    assert isinstance(full.printer, PrinterClient)
    assert isinstance(full.transcriber, SpeechClient)
    assert isinstance(full.interpreter, ClaudeInterpreter)
    close_providers(full)


def test_json_logs_carry_the_request_id() -> None:
    handler = configure_logging("INFO", "json")
    stream = io.StringIO()
    handler.setStream(stream)  # type: ignore[attr-defined]
    token = request_id.set("req-1")
    try:
        logging.getLogger("teva_delivery.test").info("hello", extra={"event": {"count": 1}})
    finally:
        request_id.reset(token)
    entry = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert entry["request_id"] == "req-1"
    assert entry["event"] == {"count": 1}
    assert logging.getLogger("httpx").level == logging.WARNING
    configure_logging("WARNING", "text")
