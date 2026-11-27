"""Cargo shipments: create once, link existing ones, reconcile the uncertain, follow status.

With the ``konimbo`` source (the default) Konimbo's Cargo integration creates the shipment
and the service only records the number it finds in the order, or that a person types in.

With the ``cargo`` source the service creates it. Cargo has no idempotency key and no lookup
by order, so a duplicate shipment can only be prevented here. Every attempt is a row written
*before* the request leaves; the request is sent at most once per row; an answer that never
came makes the row ``uncertain``, and an uncertain row blocks new attempts until a person
checks Cargo and links the shipment or confirms that none was created.

A shipment number is never attached to two packing units.
"""

import logging
import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.addresses import parse_address
from ..domain.enums import (
    LIVE_SHIPMENT_STATES,
    USABLE_SHIPMENT_STATES,
    FulfillmentState,
    JobKind,
    JobState,
    NoteKind,
    ShipmentSource,
    ShipmentState,
    ShippingProfile,
)
from ..domain.errors import ProviderError, ServiceError
from ..domain.identity import SYSTEM_ACTOR, Principal
from ..domain.ports import PostalAddress, ShipmentRequest, ShipmentStatus
from ..persistence.models import Fulfillment, Job, Shipment
from .completion import Completion, latest_shipment, primary_order
from .context import Context
from .records import audit, changed, enqueue, get_fulfillment, present

logger = logging.getLogger("teva_delivery.shipments")

_SHIPMENT_NUMBER = re.compile(r"\d{4,12}")
# Cargo status codes (from Cargo's own plugin): 3 delivered, 8 cancelled.
DELIVERED = 3
CANCELLED = 8
MAX_NOTES = 400


def request_shipment(
    session: Session, context: Context, fulfillment: Fulfillment, *, actor: str
) -> Shipment:
    """Record a new shipment attempt and queue the request to Cargo."""
    if fulfillment.shipping_profile == ShippingProfile.NONE:
        raise ServiceError("shipment_not_required")
    if context.config().shipment_source != "cargo":
        raise ServiceError("shipments_by_konimbo")
    if context.providers.carrier is None:
        raise ServiceError("cargo_not_configured")
    if not context.can_create_shipments:
        raise ServiceError("cargo_writes_disabled")
    current = latest_shipment(session, fulfillment.id)
    if current is not None and current.state in LIVE_SHIPMENT_STATES:
        raise ServiceError(
            "shipment_uncertain" if current.state == ShipmentState.UNCERTAIN else "shipment_exists"
        )
    now = context.now()
    shipment = Shipment(
        fulfillment_id=fulfillment.id,
        state=ShipmentState.REQUESTED,
        source=ShipmentSource.CARGO,
        box=fulfillment.shipping_profile == ShippingProfile.BOX,
        requested_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(shipment)
    session.flush()
    job = enqueue(
        session,
        JobKind.CARGO_CREATE_SHIPMENT,
        f"shipment:{shipment.id}",
        {"shipment_id": shipment.id},
        now=now,
        max_attempts=context.settings.job_max_attempts,
        fulfillment_id=fulfillment.id,
        order_id=fulfillment.primary_order_id,
    )
    shipment.job_id = job.id
    audit(
        session,
        "shipment.requested",
        actor=actor,
        now=now,
        fulfillment_id=fulfillment.id,
        shipment_id=shipment.id,
    )
    return shipment


class Shipments:
    def __init__(self, context: Context, completion: Completion) -> None:
        self.context = context
        self.completion = completion

    def build_request(self, session: Session, fulfillment: Fulfillment) -> ShipmentRequest:
        """What Cargo is sent. Raises a coded error when something the courier needs is missing."""
        settings = self.context.settings
        box = fulfillment.shipping_profile == ShippingProfile.BOX
        if box:
            # Konimbo's order API does not say which BOX point the customer chose.
            raise ServiceError("pickup_point_unknown")
        customer_code = settings.cargo_customer_code_express
        if customer_code is None:
            raise ServiceError("cargo_customer_code_missing")
        if not settings.cargo_sender_complete:
            raise ServiceError("cargo_sender_incomplete")
        order = primary_order(fulfillment)
        address = parse_address(order.address)
        if address is None:
            raise ServiceError("address_incomplete")
        if not order.phone:
            raise ServiceError("phone_missing")
        name = order.customer_name or "לקוח"
        delivery = [note.text for note in fulfillment.notes if note.kind == NoteKind.DELIVERY]
        references = [f"הזמנה {item.id}" for item in fulfillment.orders]
        notes = " | ".join([*delivery, ", ".join(references)])[:MAX_NOTES]
        total = sum((item.total_price or Decimal(0) for item in fulfillment.orders), Decimal(0))
        sender_street = " ".join(
            part for part in (settings.cargo_from_street, settings.cargo_from_street_number) if part
        )
        return ShipmentRequest(
            reference=order.id,
            customer_code=customer_code,
            box=box,
            recipient=PostalAddress(
                name=name,
                company=name,
                street=address.street,
                city=address.city,
                phone=order.phone,
                email=order.email or "",
                floor=address.floor,
                apartment=address.apartment,
            ),
            sender=PostalAddress(
                name=settings.cargo_from_name,
                company=settings.cargo_from_name,
                street=sender_street,
                city=settings.cargo_from_city,
                phone=settings.cargo_from_phone,
            ),
            total_value=total,
            notes=notes,
        )

    # Worker side.

    def run_create(self, job_id: int) -> None:
        context = self.context
        carrier = context.providers.carrier
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None or job.state != JobState.RUNNING:
                return
            shipment = session.get(Shipment, job.payload["shipment_id"], with_for_update=True)
            if shipment is None or shipment.state != ShipmentState.REQUESTED:
                self._finish(job, JobState.CANCELLED, None)
                return
            now = context.now()
            if shipment.dispatched_at is not None:
                # An earlier attempt was sent and never answered (the process stopped).
                self._uncertain(session, job, shipment)
                return
            fulfillment = get_fulfillment(session, shipment.fulfillment_id)
            try:
                if carrier is None:
                    raise ServiceError("cargo_not_configured")
                if not context.can_create_shipments:
                    raise ServiceError("cargo_writes_disabled")
                request = self.build_request(session, fulfillment)
            except ServiceError as error:
                self._failed(session, job, shipment, error.code)
                return
            shipment.customer_code = request.customer_code
            shipment.dispatched_at = now
            shipment.updated_at = now
        # The attempt is on record before anything leaves the service.
        carrier = present(carrier)
        try:
            created = carrier.create_shipment(request)
        except ProviderError as error:
            self._after_error(job_id, shipment.id, error)
            return
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            shipment = session.get(Shipment, shipment.id, with_for_update=True)
            job, shipment = present(job), present(shipment)
            now = context.now()
            duplicate = session.scalars(
                select(Shipment).where(
                    Shipment.external_id == created.external_id, Shipment.id != shipment.id
                )
            ).first()
            if duplicate is not None:
                # Cargo answered with a number another order already holds: never reuse it.
                logger.error(
                    "Cargo returned a shipment number that is already linked",
                    extra={"event": {"shipment": shipment.id, "other": duplicate.id}},
                )
                self._uncertain(session, job, shipment)
                return
            shipment.state = ShipmentState.CREATED
            shipment.external_id = created.external_id
            shipment.status_code = created.status_code
            shipment.status_text = created.status_text
            shipment.line_text = created.line_text
            shipment.updated_at = now
            shipment.status_synced_at = now
            self._finish(job, JobState.SUCCEEDED, {"shipment": created.external_id})
            fulfillment = get_fulfillment(session, shipment.fulfillment_id, lock=True)
            audit(
                session,
                "shipment.created",
                actor=SYSTEM_ACTOR,
                now=now,
                fulfillment_id=fulfillment.id,
                shipment=created.external_id,
            )
            self.completion.advance(session, fulfillment)
            changed(session, context.events, fulfillment, now)

    def _after_error(self, job_id: int, shipment_id: int, error: ProviderError) -> None:
        context = self.context
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            shipment = session.get(Shipment, shipment_id, with_for_update=True)
            job, shipment = present(job), present(shipment)
            if not error.sent:
                # Nothing reached Cargo: safe to send again later.
                shipment.dispatched_at = None
                if job.attempts < job.max_attempts and error.retryable:
                    self._retry_later(job, error.code)
                else:
                    self._failed(session, job, shipment, error.code)
                return
            if error.code == "cargo_rejected":
                self._failed(session, job, shipment, error.code)
                return
            self._uncertain(session, job, shipment, error.code)

    def _retry_later(self, job: Job, code: str) -> None:
        now = self.context.now()
        job.state = JobState.PENDING
        job.last_error = code
        job.lease_until = None
        job.run_after = now + timedelta(seconds=min(30 * 2 ** max(job.attempts - 1, 0), 1800))
        job.updated_at = now

    def _finish(self, job: Job, state: JobState, result: dict[str, object] | None) -> None:
        now = self.context.now()
        job.state = state
        job.result = result
        job.lease_until = None
        job.updated_at = now
        job.finished_at = now

    def _failed(self, session: Session, job: Job, shipment: Shipment, code: str) -> None:
        now = self.context.now()
        shipment.state = ShipmentState.FAILED
        shipment.error_code = code
        shipment.updated_at = now
        job.last_error = code
        self._finish(job, JobState.FAILED, None)
        audit(
            session,
            "shipment.failed",
            actor=SYSTEM_ACTOR,
            now=now,
            fulfillment_id=shipment.fulfillment_id,
            code=code,
        )
        self._changed(session, shipment.fulfillment_id)

    def _uncertain(
        self, session: Session, job: Job, shipment: Shipment, code: str = "shipment_uncertain"
    ) -> None:
        now = self.context.now()
        shipment.state = ShipmentState.UNCERTAIN
        shipment.error_code = "shipment_uncertain"
        shipment.updated_at = now
        job.last_error = code
        self._finish(job, JobState.UNCERTAIN, None)
        audit(
            session,
            "shipment.uncertain",
            actor=SYSTEM_ACTOR,
            now=now,
            fulfillment_id=shipment.fulfillment_id,
            code=code,
        )
        self._changed(session, shipment.fulfillment_id)

    def _changed(self, session: Session, fulfillment_id: int) -> None:
        fulfillment = get_fulfillment(session, fulfillment_id)
        changed(session, self.context.events, fulfillment, self.context.now())

    # Employee side.

    def create(self, actor: Principal, fulfillment_id: int) -> None:
        """Send this order to Cargo now (before packing ends), or again after a refusal."""
        context = self.context
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state in (FulfillmentState.CANCELLED, FulfillmentState.COMPLETED):
                raise ServiceError("invalid_state")
            request_shipment(session, context, fulfillment, actor=actor.id)
            changed(session, context.events, fulfillment, context.now())

    def create_batch(self, actor: Principal, fulfillment_ids: list[int]) -> dict[str, list[int]]:
        """Batch transmission: one queued request per order, each with its own record."""
        queued: list[int] = []
        skipped: list[int] = []
        for fulfillment_id in fulfillment_ids:
            try:
                self.create(actor, fulfillment_id)
            except ServiceError:
                skipped.append(fulfillment_id)
            else:
                queued.append(fulfillment_id)
        return {"queued": queued, "skipped": skipped}

    def link(self, actor: Principal, fulfillment_id: int, external_id: str) -> None:
        """Attach a shipment that already exists in Cargo, typed in by an employee.

        When Cargo is connected, Cargo must confirm the number first. Without Cargo the number
        is recorded as given; the label check before printing still ties it to this order.
        """
        context = self.context
        number = external_id.strip()
        if not _SHIPMENT_NUMBER.fullmatch(number):
            raise ServiceError("shipment_number_invalid")
        with context.db.session() as session:
            fulfillment = get_fulfillment(session, fulfillment_id)
            if fulfillment.shipping_profile == ShippingProfile.NONE:
                raise ServiceError("shipment_not_required")
            if fulfillment.state == FulfillmentState.CANCELLED:
                raise ServiceError("invalid_state")
            box = fulfillment.shipping_profile == ShippingProfile.BOX
        customer_code, status = self.verify(number, box=box)
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            self.attach(
                session,
                fulfillment,
                number,
                source=ShipmentSource.MANUAL,
                actor=actor.id,
                customer_code=customer_code,
                status_code=status.code if status else None,
                status_text=status.text if status else None,
            )

    def verify(self, number: str, *, box: bool) -> tuple[int | None, ShipmentStatus | None]:
        """Ask Cargo about a number, when Cargo is connected. Raises when it is unusable."""
        carrier = self.context.providers.carrier
        settings = self.context.settings
        customer_code = (
            (settings.cargo_customer_code_box if box else settings.cargo_customer_code_express)
            or settings.cargo_customer_code_express
            or settings.cargo_customer_code_box
        )
        if carrier is None or customer_code is None:
            return customer_code, None
        try:
            status = carrier.get_status(number, customer_code)
        except ProviderError as error:
            raise ServiceError(error.code) from None
        if status.code == CANCELLED:
            raise ServiceError("shipment_cancelled")
        return customer_code, status

    def attach(
        self,
        session: Session,
        fulfillment: Fulfillment,
        number: str,
        *,
        source: ShipmentSource,
        actor: str,
        customer_code: int | None,
        status_code: int | None = None,
        status_text: str | None = None,
    ) -> Shipment:
        """Record an existing shipment number for this packing unit, exactly once."""
        context = self.context
        other = session.scalars(select(Shipment).where(Shipment.external_id == number)).first()
        if other is not None and other.fulfillment_id != fulfillment.id:
            raise ServiceError("shipment_exists")
        now = context.now()
        current = latest_shipment(session, fulfillment.id)
        if current is not None and current.state in USABLE_SHIPMENT_STATES:
            if current.external_id == number:
                return current
            raise ServiceError("shipment_exists")
        if other is not None:
            # This unit's own earlier row for the number (failed or cancelled): not reused.
            raise ServiceError("shipment_cancelled")
        if (
            current is not None
            and current.state == ShipmentState.REQUESTED
            and current.dispatched_at is None
        ):
            # Not sent yet: cancel the queued request, the linked one replaces it.
            self._cancel_request(session, current)
            current = None
        if current is not None and current.state == ShipmentState.REQUESTED:
            # Sent and unanswered: the number may be that request's answer, but it may not.
            raise ServiceError("shipment_pending")
        if current is not None and current.state == ShipmentState.UNCERTAIN:
            shipment = current
        else:
            shipment = Shipment(
                fulfillment_id=fulfillment.id,
                requested_by=actor,
                box=fulfillment.shipping_profile == ShippingProfile.BOX,
                created_at=now,
                state=ShipmentState.LINKED,
                updated_at=now,
            )
            session.add(shipment)
        shipment.state = ShipmentState.LINKED
        shipment.source = source
        shipment.external_id = number
        shipment.customer_code = customer_code
        shipment.status_code = status_code
        shipment.status_text = status_text
        shipment.error_code = None
        shipment.updated_at = now
        shipment.status_synced_at = now if status_code is not None else None
        session.flush()
        audit(
            session,
            "shipment.linked",
            actor=actor,
            now=now,
            fulfillment_id=fulfillment.id,
            shipment=number,
            source=str(source),
        )
        self.completion.advance(session, fulfillment)
        changed(session, context.events, fulfillment, now)
        return shipment

    def _cancel_request(self, session: Session, shipment: Shipment) -> None:
        shipment.state = ShipmentState.CANCELLED
        shipment.updated_at = self.context.now()
        if shipment.job_id is not None:
            job = session.get(Job, shipment.job_id, with_for_update=True)
            if job is not None and job.state == JobState.PENDING:
                self._finish(job, JobState.CANCELLED, None)

    def resolve_not_created(self, actor: Principal, fulfillment_id: int) -> None:
        """A person checked Cargo: the uncertain shipment does not exist. Try again."""
        context = self.context
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            current = latest_shipment(session, fulfillment_id)
            if current is None or current.state != ShipmentState.UNCERTAIN:
                raise ServiceError("invalid_state")
            now = context.now()
            current.state = ShipmentState.FAILED
            current.error_code = "reconciled_not_created"
            current.updated_at = now
            audit(
                session,
                "shipment.reconciled",
                actor=actor.id,
                now=now,
                fulfillment_id=fulfillment_id,
                outcome="not_created",
            )
            if fulfillment.state == FulfillmentState.PACKED and context.can_create_shipments:
                request_shipment(session, context, fulfillment, actor=actor.id)
            changed(session, context.events, fulfillment, now)

    # Status follow-up (scheduler).

    def sync_statuses(self) -> int:
        context = self.context
        carrier = context.providers.carrier
        minutes = context.settings.cargo_status_sync_minutes
        if carrier is None or minutes <= 0:
            return 0
        now = context.now()
        due = now - timedelta(minutes=minutes)
        with context.db.session() as session:
            rows = list(
                session.scalars(
                    select(Shipment)
                    .where(Shipment.state.in_(list(USABLE_SHIPMENT_STATES)))
                    .where(
                        Shipment.status_code.is_(None)
                        | Shipment.status_code.not_in([DELIVERED, CANCELLED])
                    )
                    .where(Shipment.status_synced_at.is_(None) | (Shipment.status_synced_at < due))
                    .order_by(Shipment.status_synced_at)
                    .limit(20)
                )
            )
            settings = context.settings
            fallback = settings.cargo_customer_code_express or settings.cargo_customer_code_box
            targets = [(row.id, row.external_id, row.customer_code or fallback) for row in rows]
        updated = 0
        for shipment_id, external_id, customer_code in targets:
            if external_id is None or customer_code is None:
                continue
            try:
                status = carrier.get_status(external_id, customer_code)
            except ProviderError:
                continue
            with context.db.session(write=True) as session:
                shipment = session.get(Shipment, shipment_id, with_for_update=True)
                if shipment is None:
                    continue
                shipment.status_code = status.code
                shipment.status_text = status.text
                shipment.status_synced_at = context.now()
                shipment.customer_code = shipment.customer_code or customer_code
                if status.code == CANCELLED:
                    shipment.state = ShipmentState.CANCELLED
                    fulfillment = get_fulfillment(session, shipment.fulfillment_id, lock=True)
                    fulfillment.upstream_warning = "shipment_cancelled"
                    audit(
                        session,
                        "shipment.cancelled_upstream",
                        actor=SYSTEM_ACTOR,
                        now=context.now(),
                        fulfillment_id=fulfillment.id,
                    )
                    changed(session, context.events, fulfillment, context.now())
                updated += 1
        return updated
