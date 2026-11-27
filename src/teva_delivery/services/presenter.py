"""Builds the read models the screens get, from the rows. Nothing here writes."""

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import packing
from ..domain.enums import (
    CATEGORY_LABELS,
    USABLE_SHIPMENT_STATES,
    Category,
    FulfillmentState,
    JobKind,
    JobState,
    LineStatus,
    NoteKind,
    NoteSource,
    PrintMode,
    PrintState,
    ShipmentState,
    ShippingProfile,
)
from ..domain.errors import message_for
from ..domain.identity import Principal
from ..persistence.models import (
    Fulfillment,
    Job,
    Line,
    Note,
    Order,
    PrintJob,
    Product,
    ProductAlias,
    Shipment,
)
from .completion import Completion, latest_shipment, prints_for
from .context import Context
from .records import held_by, jobs_for
from .views import (
    ClarificationView,
    FulfillmentView,
    Holder,
    JobView,
    LineView,
    NoteView,
    OrderSummary,
    PrintView,
    Progress,
    QueueItem,
    ShipmentView,
)
from .warehouse_config import WarehouseConfig

RETRYABLE_KINDS = frozenset(
    {JobKind.KONIMBO_STATUS, JobKind.KONIMBO_NOTE, JobKind.KONIMBO_FIND_SHIPMENT}
)


def item_key(line: Line) -> str | None:
    if line.item_id:
        return f"item:{line.item_id}"
    if line.sku:
        return f"sku:{line.sku}"
    return None


def active_lines(fulfillment: Fulfillment) -> list[Line]:
    return [line for line in fulfillment.lines if not line.removed]


def line_facts(line: Line) -> packing.LineFacts:
    return packing.LineFacts(
        id=line.id,
        title=line.title,
        required=line.required,
        confirmed=line.confirmed,
        missing=line.missing,
    )


def note_facts(note: Note) -> packing.NoteFacts:
    return packing.NoteFacts(
        id=note.id, kind=NoteKind(note.kind), confirmed=note.confirmed_at is not None
    )


def progress_of(fulfillment: Fulfillment) -> packing.Progress:
    return packing.progress(
        (line_facts(line) for line in active_lines(fulfillment)),
        (note_facts(note) for note in fulfillment.notes),
    )


def progress_view(value: packing.Progress) -> Progress:
    return Progress(
        units_confirmed=value.units_confirmed,
        units_required=value.units_required,
        lines_confirmed=value.lines_confirmed,
        lines_total=value.lines_total,
        notes_open=value.notes_open,
        lines_missing=value.lines_missing,
        complete=value.complete,
    )


def payment_ok(config: WarehouseConfig, status: str | None) -> bool | None:
    if status and status in config.failed_payment_statuses:
        return False
    if not config.paid_statuses:
        return None
    return status in config.paid_statuses


def holder_view(fulfillment: Fulfillment, viewer: Principal, now: datetime) -> Holder | None:
    if fulfillment.assigned_to is None or fulfillment.state != FulfillmentState.PACKING:
        return None
    if fulfillment.lease_until is None or fulfillment.lease_until <= now:
        return None
    return Holder(
        actor=fulfillment.assigned_to,
        mine=fulfillment.assigned_to == viewer.id,
        lease_until=fulfillment.lease_until,
    )


def job_view(job: Job) -> JobView:
    retryable = job.kind in RETRYABLE_KINDS and job.state in (JobState.FAILED, JobState.UNCERTAIN)
    return JobView(
        id=job.id,
        kind=job.kind,
        state=JobState(job.state),
        order_id=job.order_id,
        attempts=job.attempts,
        last_error=job.last_error,
        last_error_message=message_for(job.last_error) if job.last_error else None,
        dry_run=job.dry_run,
        purpose=job.payload.get("purpose"),
        created_at=job.created_at,
        updated_at=job.updated_at,
        retryable=retryable,
    )


def order_summary(order: Order, config: WarehouseConfig) -> OrderSummary:
    return OrderSummary(
        id=order.id,
        customer_name=order.customer_name,
        payment_status=order.payment_status,
        payment_ok=payment_ok(config, order.payment_status),
        shipping_title=order.shipping_title,
        status_title=order.status_title,
        created_at=order.store_created_at,
    )


class Presenter:
    def __init__(self, context: Context, completion: Completion) -> None:
        self.context = context
        self.completion = completion

    def fulfillment(
        self, session: Session, fulfillment: Fulfillment, viewer: Principal
    ) -> FulfillmentView:
        context = self.context
        config = context.config()
        now = context.now()
        lines = fulfillment.lines
        images = self._images(session, lines)
        aliases = self._aliases(session, lines)
        shipment = latest_shipment(session, fulfillment.id)
        prints = prints_for(session, shipment.id) if shipment is not None else []
        usable = shipment is not None and shipment.state in USABLE_SHIPMENT_STATES
        state = FulfillmentState(fulfillment.state)
        clarification = None
        if (
            fulfillment.clarification
            and fulfillment.clarification_until is not None
            and fulfillment.clarification_until > now
        ):
            clarification = ClarificationView(
                question=str(fulfillment.clarification.get("question", "")),
                candidates=[
                    int(value) for value in fulfillment.clarification.get("candidates", [])
                ],
            )
        auto_print_due = (
            config.auto_print
            and context.can_print_labels
            and usable
            and not prints
            and state in (FulfillmentState.PACKED, FulfillmentState.COMPLETED)
            and fulfillment.packed_by == viewer.id
        )
        return FulfillmentView(
            id=fulfillment.id,
            version=fulfillment.version,
            state=state,
            category=Category(fulfillment.category),
            category_label=CATEGORY_LABELS[Category(fulfillment.category)],
            urgent=fulfillment.category == Category.URGENT,
            shipping_profile=ShippingProfile(fulfillment.shipping_profile),
            review_reason=fulfillment.review_reason,
            skip_reason=fulfillment.skip_reason,
            upstream_warning=fulfillment.upstream_warning,
            orders=[order_summary(order, config) for order in self._orders(fulfillment)],
            lines=[self._line(line, images, aliases) for line in lines],
            notes=[self._note(note) for note in fulfillment.notes],
            progress=progress_view(progress_of(fulfillment)),
            holder=holder_view(fulfillment, viewer, now),
            editable=state == FulfillmentState.PACKING and held_by(fulfillment, viewer.id, now),
            shipment=self._shipment(shipment) if shipment is not None else None,
            prints=[self._print(job) for job in prints],
            steps=self.completion.steps(session, fulfillment),
            jobs=[job_view(job) for job in jobs_for(session, fulfillment.id)],
            clarification=clarification,
            queued_at=fulfillment.queued_at,
            packed_at=fulfillment.packed_at,
            completed_at=fulfillment.completed_at,
            auto_print_due=auto_print_due,
        )

    def queue_item(
        self,
        fulfillment: Fulfillment,
        viewer: Principal,
        *,
        problem: str | None = None,
    ) -> QueueItem:
        orders = self._orders(fulfillment)
        lines = active_lines(fulfillment)
        return QueueItem(
            id=fulfillment.id,
            state=FulfillmentState(fulfillment.state),
            category=Category(fulfillment.category),
            urgent=fulfillment.category == Category.URGENT,
            order_ids=[order.id for order in orders],
            customer_name=orders[0].customer_name if orders else None,
            lines=len(lines),
            units=sum(line.required for line in lines),
            progress=progress_view(progress_of(fulfillment)),
            holder=holder_view(fulfillment, viewer, self.context.now()),
            queued_at=fulfillment.queued_at,
            review_reason=fulfillment.review_reason,
            skip_reason=fulfillment.skip_reason,
            problem=problem or fulfillment.upstream_warning,
        )

    # Pieces.

    def _orders(self, fulfillment: Fulfillment) -> Sequence[Order]:
        return sorted(
            fulfillment.orders,
            key=lambda order: (order.id != fulfillment.primary_order_id, order.store_created_at),
        )

    def _images(self, session: Session, lines: Iterable[Line]) -> dict[str, str | None]:
        ids = {line.item_id for line in lines if line.item_id}
        if not ids or not self.context.settings.product_images:
            return {}
        rows = session.scalars(select(Product).where(Product.item_id.in_(ids))).all()
        return {row.item_id: row.image_url for row in rows}

    def _aliases(self, session: Session, lines: Iterable[Line]) -> dict[str, list[str]]:
        keys = {key for line in lines if (key := item_key(line))}
        if not keys:
            return {}
        result: dict[str, list[str]] = {}
        rows = session.scalars(
            select(ProductAlias).where(ProductAlias.item_key.in_(keys)).order_by(ProductAlias.id)
        )
        for row in rows:
            result.setdefault(row.item_key, []).append(row.alias)
        return result

    def _line(
        self, line: Line, images: dict[str, str | None], aliases: dict[str, list[str]]
    ) -> LineView:
        facts = line_facts(line)
        key = item_key(line)
        return LineView(
            id=line.id,
            order_id=line.order_id,
            title=line.title,
            sku=line.sku,
            image_url=images.get(line.item_id) if line.item_id else None,
            kind=line.kind,
            required=line.required,
            confirmed=line.confirmed,
            remaining=facts.remaining,
            missing=line.missing,
            missing_reason=line.missing_reason,
            status=LineStatus(facts.status),
            removed=line.removed,
            aliases=aliases.get(key, []) if key else [],
        )

    def _note(self, note: Note) -> NoteView:
        kind = NoteKind(note.kind)
        return NoteView(
            id=note.id,
            order_id=note.order_id,
            source=NoteSource(note.source),
            kind=kind,
            text=note.text,
            requires_confirmation=kind is NoteKind.PACKING,
            confirmed=note.confirmed_at is not None,
            confirmed_at=note.confirmed_at,
        )

    def _shipment(self, shipment: Shipment) -> ShipmentView:
        usable = shipment.state in USABLE_SHIPMENT_STATES
        return ShipmentView(
            id=shipment.id,
            state=ShipmentState(shipment.state),
            external_id=shipment.external_id,
            box=shipment.box,
            status_code=shipment.status_code,
            status_text=shipment.status_text,
            line_text=shipment.line_text,
            error_code=shipment.error_code,
            error_message=message_for(shipment.error_code) if shipment.error_code else None,
            label_ready=usable and self.context.can_print_labels,
            created_at=shipment.created_at,
        )

    def _print(self, job: PrintJob) -> PrintView:
        return PrintView(
            id=job.id,
            mode=PrintMode(job.mode),
            state=PrintState(job.state),
            printer_id=job.printer_id,
            printer_job_id=job.printer_job_id,
            warning=job.warning,
            error_code=job.error_code,
            created_at=job.created_at,
        )
