"""Helpers the tests share: settings, the container, and walking an order through packing."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from teva_delivery.core.clock import ManualClock
from teva_delivery.core.config import Settings
from teva_delivery.domain.enums import Action
from teva_delivery.domain.identity import Principal
from teva_delivery.domain.ports import PrintCredentials
from teva_delivery.persistence.database import Database
from teva_delivery.providers.factory import build_providers
from teva_delivery.services.container import Services, build_services
from teva_delivery.services.context import Context, Providers
from teva_delivery.services.packing import ActionInput
from teva_delivery.services.views import ActionResult, FulfillmentView

START = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
DANA = Principal("dana")
YOSSI = Principal("yossi")
ADMIN = Principal("manager", "admin")
NO_CREDENTIALS = PrintCredentials()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mode": "demo",
        "auth_disabled": True,
        "data_dir": tmp_path,
        "worker_enabled": False,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]  # pydantic-settings option


def build(settings: Settings, clock: ManualClock, providers: Providers | None = None) -> Services:
    db = Database(settings.database_url)
    db.migrate()
    context = Context(settings=settings, db=db, providers=Providers(), clock=clock)
    context.providers = providers or build_providers(settings, clock, context.config)
    return build_services(context)


def act(
    services: Services, actor: Principal, fulfillment_id: int, action: Action, **fields: Any
) -> ActionResult:
    return services.work.act(
        actor, fulfillment_id, ActionInput(action=action, **fields), credentials=NO_CREDENTIALS
    )


def pack_everything(services: Services, actor: Principal, view: FulfillmentView) -> None:
    """Confirm every product and packing note of an opened order."""
    for line in view.lines:
        if line.remaining and not line.removed:
            act(services, actor, view.id, Action.CONFIRM_PRODUCT, line_id=line.id, everything=True)
    for note in view.notes:
        if note.requires_confirmation and not note.confirmed:
            act(services, actor, view.id, Action.CONFIRM_ORDER_NOTE, note_id=note.id)


def settle(services: Services, clock: ManualClock, *, rounds: int = 8, step: float = 30) -> None:
    """Let the worker run its jobs, moving the clock past their delays."""
    for _ in range(rounds):
        clock.advance(step)
        services.worker.drain()


def steps(view: FulfillmentView) -> dict[str, str]:
    return {step.key: step.state for step in view.steps}
