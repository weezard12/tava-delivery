"""What the screens show about the service itself: readiness, sync health and settings."""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from .. import __version__
from ..domain.enums import ACTIVE_JOB_STATES, CATEGORY_LABELS, JobState
from ..domain.errors import ProviderError, ServiceError
from ..domain.hebrew.commands import HelpText
from ..domain.identity import Principal
from ..domain.ports import PrintCredentials
from ..persistence.models import Job, StatusOption, SyncRun
from .context import Context
from .presenter import job_view
from .records import audit
from .sync import Sync
from .views import InfoView, Readiness, SyncRunView, SyncView
from .warehouse_config import WarehouseConfig

RECENT_RUNS = 20
LISTED_JOBS = 50


class StatusOptionView(BaseModel):
    title: str
    orders_seen: int
    in_use: bool


class SettingsView(BaseModel):
    config: WarehouseConfig
    defaults: WarehouseConfig
    updated_at: str | None
    updated_by: str | None
    status_options: list[StatusOptionView]


def run_view(row: SyncRun) -> SyncRunView:
    return SyncRunView(
        kind=row.kind,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=row.outcome,
        error=row.error,
        counts=dict(row.counts),
    )


class Overview:
    def __init__(self, context: Context, sync: Sync) -> None:
        self.context = context
        self.sync = sync

    def info(self) -> InfoView:
        context = self.context
        settings = context.settings
        providers = context.providers
        config = context.config()
        demo = context.demo

        konimbo_missing = [] if providers.store else ["DELIVERY_KONIMBO_TOKEN"]
        cargo_missing: list[str] = []
        if providers.carrier is None:
            cargo_missing.append("DELIVERY_CARGO_TOKEN")
        if not demo and config.shipment_source == "cargo":
            if not settings.cargo_configured:
                cargo_missing.append("DELIVERY_CARGO_CUSTOMER_CODE_EXPRESS")
            if not settings.cargo_sender_complete:
                cargo_missing.append("DELIVERY_CARGO_FROM_*")
        printer_missing: list[str] = []
        if providers.printer is None:
            printer_missing.append("DELIVERY_PRINTER_URL")
        if not config.label_printer_id:
            printer_missing.append("label_printer_id")
        return InfoView(
            mode=settings.mode,
            version=__version__,
            timezone=settings.timezone,
            konimbo=Readiness(
                ready=providers.store is not None,
                missing=konimbo_missing,
                writes=not context.store_dry_run,
            ),
            cargo=Readiness(
                ready=not cargo_missing,
                missing=cargo_missing,
                writes=context.can_create_shipments,
            ),
            printer=Readiness(ready=context.can_print_labels, missing=printer_missing),
            speech=Readiness(
                ready=providers.transcriber is not None,
                missing=[] if providers.transcriber else ["DELIVERY_STT_URL"],
            ),
            ai=Readiness(
                ready=providers.interpreter is not None,
                missing=[] if providers.interpreter else ["DELIVERY_AI_PROVIDER"],
            ),
            auto_print=config.auto_print,
            require_label_print=config.require_label_print,
            label_printer_id=config.label_printer_id or None,
            categories={category.value: label for category, label in CATEGORY_LABELS.items()},
            statuses={
                "awaiting_inventory": config.status_awaiting_inventory,
                "consolidation": config.status_consolidation,
                "urgent": config.status_urgent,
                "pickup_points": config.status_pickup_points,
                "regular": ", ".join(config.status_regular),
                "preparing": config.status_preparing,
                "transmit": config.status_transmit,
                "awaiting_pickup": config.status_awaiting_pickup,
                "note": config.note_title,
            },
            voice_examples=list(HelpText().examples),
        )

    def sync_view(self) -> SyncView:
        store = self.context.providers.store
        with self.context.db.session() as session:
            runs = session.scalars(
                select(SyncRun).order_by(SyncRun.started_at.desc()).limit(RECENT_RUNS)
            ).all()

            def jobs(state: JobState) -> list[Job]:
                return list(
                    session.scalars(
                        select(Job)
                        .where(Job.state == state)
                        .order_by(Job.created_at.desc())
                        .limit(LISTED_JOBS)
                    )
                )

            pending = session.scalar(
                select(func.count(Job.id)).where(Job.state.in_(ACTIVE_JOB_STATES))
            )
            return SyncView(
                runs=[run_view(row) for row in runs],
                failed_jobs=[job_view(job) for job in jobs(JobState.FAILED)],
                uncertain_jobs=[job_view(job) for job in jobs(JobState.UNCERTAIN)],
                pending_jobs=pending or 0,
                konimbo_requests_left=store.requests_left if store is not None else None,
            )

    def run_sync(self) -> SyncRunView:
        return run_view(self.sync.run())

    # Settings.

    def settings(self) -> SettingsView:
        context = self.context
        config = context.config()
        in_use = {
            *config.category_by_status,
            config.status_preparing,
            config.status_transmit,
            config.status_awaiting_pickup,
            config.note_title,
        }
        with context.db.session() as session:
            rows = session.scalars(
                select(StatusOption).order_by(StatusOption.orders_seen.desc(), StatusOption.title)
            ).all()
            options = [
                StatusOptionView(
                    title=row.title, orders_seen=row.orders_seen, in_use=row.title in in_use
                )
                for row in rows
            ]
        updated = context.runtime.updated()
        return SettingsView(
            config=config,
            defaults=context.runtime.defaults,
            updated_at=updated[0].isoformat() if updated else None,
            updated_by=updated[1] if updated else None,
            status_options=options,
        )

    def update_settings(self, actor: Principal, changes: dict[str, Any]) -> SettingsView:
        if not actor.admin:
            raise ServiceError("admin_required")
        context = self.context
        unknown = set(changes) - set(WarehouseConfig.model_fields)
        if unknown:
            raise ServiceError("settings_invalid")
        now = context.now()
        context.runtime.update(changes, actor=actor.id, now=now)
        with context.db.session(write=True) as session:
            audit(session, "settings.updated", actor=actor.id, now=now, fields=sorted(changes))
        context.events.queue_changed()
        return self.settings()

    def discover_statuses(self, actor: Principal) -> SettingsView:
        if not actor.admin:
            raise ServiceError("admin_required")
        if self.context.providers.store is None:
            raise ServiceError("konimbo_not_configured")
        self.sync.discover_statuses()
        return self.settings()

    def printers(self, actor: Principal, credentials: PrintCredentials) -> list[dict[str, Any]]:
        printer = self.context.providers.printer
        if printer is None:
            raise ServiceError("printer_not_configured")
        try:
            return printer.printers(credentials)
        except ProviderError as error:
            raise ServiceError(error.code) from None
