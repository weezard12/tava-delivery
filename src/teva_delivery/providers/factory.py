"""Builds the providers the settings ask for. A provider that is not configured stays ``None``.

Demo mode uses the in-memory store, Cargo and printer. Live mode connects each real service
only when its settings are present, so the service starts (and shows what is missing) before
the Konimbo and Cargo keys arrive. Speech recognition and the AI interpreter work in both.
"""

from collections.abc import Callable
from contextlib import suppress

from ..core.clock import Clock
from ..core.config import Settings
from ..services.context import Providers
from ..services.warehouse_config import WarehouseConfig
from .cargo import CargoClient
from .demo import DemoCarrier, DemoPrinter, DemoStore
from .interpreter import ClaudeInterpreter
from .konimbo import KonimboClient
from .printer import PrinterClient
from .speech import SpeechClient


def build_providers(
    settings: Settings, clock: Clock, config: Callable[[], WarehouseConfig]
) -> Providers:
    providers = Providers()
    if settings.mode == "demo":
        providers.store = DemoStore(clock, config)
        providers.carrier = DemoCarrier()
        providers.printer = DemoPrinter()
    else:
        if settings.konimbo_configured:
            providers.store = KonimboClient(
                settings.konimbo_token.get_secret_value(),
                budget=settings.konimbo_request_budget,
            )
        if settings.cargo_token.get_secret_value():
            providers.carrier = CargoClient(
                settings.cargo_token.get_secret_value(), website=settings.cargo_website
            )
        if settings.printer_url is not None:
            providers.printer = PrinterClient(
                str(settings.printer_url), token=settings.printer_token.get_secret_value()
            )
    if settings.stt_url is not None:
        providers.transcriber = SpeechClient(
            str(settings.stt_url), token=settings.stt_token.get_secret_value()
        )
    if settings.ai_provider == "anthropic":
        providers.interpreter = ClaudeInterpreter(
            settings.anthropic_api_key.get_secret_value(), model=settings.ai_model
        )
    return providers


def close_providers(providers: Providers) -> None:
    for provider in (
        providers.store,
        providers.carrier,
        providers.printer,
        providers.transcriber,
        providers.interpreter,
    ):
        close = getattr(provider, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
