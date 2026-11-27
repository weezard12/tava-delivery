"""The background writes to Konimbo, and reading the Cargo shipment number back from it.

Konimbo's order API appends statuses and has no idempotency key, so every write carries a
marker made from the job's key. Before a write that may already have been sent is sent again,
the order's history is read back: if the marker is there, the first write landed. A write is
never repeated blindly.
"""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import (
    USABLE_SHIPMENT_STATES,
    JobKind,
    JobState,
    ShipmentSource,
    ShippingProfile,
)
from ..domain.errors import ProviderError, ServiceError
from ..domain.identity import SYSTEM_ACTOR, Principal
from ..domain.ports import StoreOrder
from ..persistence.models import Fulfillment, Job, Line, Order, Shipment
from .completion import (
    OWN_MARKER,
    PURPOSE_FIND,
    PURPOSE_INVENTORY,
    PURPOSE_TRANSMIT,
    Completion,
    jobs_with,
    usable_shipment,
)
from .context import Context
from .records import audit, changed, get_fulfillment, present
from .shipments import Shipments

logger = logging.getLogger("teva_delivery.jobs")

# Konimbo's own comments are trusted from this long before the status change on.
CLOCK_SKEW = timedelta(minutes=5)


def backoff(attempts: int, *, base: float = 30, cap: float = 1800) -> timedelta:
    return timedelta(seconds=min(base * 2 ** max(attempts - 1, 0), cap))


def finish(job: Job, state: JobState, now: Any, result: dict[str, Any] | None = None) -> None:
    job.state = state
    job.result = result
    job.lease_until = None
    job.updated_at = now
    job.finished_at = now


def retry_later(job: Job, code: str, now: Any, delay: timedelta) -> None:
    job.state = JobState.PENDING
    job.last_error = code
    job.lease_until = None
    job.run_after = now + delay
    job.updated_at = now


def landed(order: StoreOrder, marker: str) -> bool:
    return any(status.comment and marker in status.comment for status in order.statuses)


def missing_summary(session: Session, fulfillment_id: int) -> str | None:
    lines = session.scalars(
        select(Line)
        .where(Line.fulfillment_id == fulfillment_id, Line.missing > 0, Line.removed.is_(False))
        .order_by(Line.position)
    ).all()
    if not lines:
        return None
    parts = []
    for line in lines:
        part = f"{line.title} ×{line.missing}"
        if line.missing_reason:
            part += f" ({line.missing_reason})"
        parts.append(part)
    return "חסר במלאי: " + "; ".join(parts)


class StoreJobs:
    """Runs ``konimbo.status``, ``konimbo.note`` and ``konimbo.find_shipment`` jobs."""

    def __init__(self, context: Context, completion: Completion, shipments: Shipments) -> None:
        self.context = context
        self.completion = completion
        self.shipments = shipments

    # Status and note writes.

    def run_write(self, job_id: int) -> None:
        context = self.context
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None or job.state != JobState.RUNNING:
                return
            payload = dict(job.payload)
            now = context.now()
            if payload.get("purpose") == PURPOSE_INVENTORY and job.fulfillment_id is not None:
                summary = missing_summary(session, job.fulfillment_id)
                if summary is None:
                    # The missing report was undone before it was sent.
                    finish(job, JobState.CANCELLED, now)
                    return
                payload["comment"] = f"{summary} {payload['marker']}"
                job.payload = payload
            if job.dry_run:
                finish(job, JobState.SUCCEEDED, now, {"dry_run": True})
                audit(
                    session,
                    "konimbo.dry_run",
                    actor=SYSTEM_ACTOR,
                    now=now,
                    fulfillment_id=job.fulfillment_id,
                    job=job.id,
                    purpose=payload.get("purpose"),
                )
                self.completion.after_store_job(session, job)
                return
            if context.providers.store is None:
                job.last_error = "konimbo_not_configured"
                finish(job, JobState.FAILED, now)
                self._notify(session, job)
                return
            maybe_sent = bool(payload.get("sent"))
            if not maybe_sent:
                # On record before the request leaves: a crash after this reads back first.
                job.payload = {**payload, "sent": True}
        store = context.providers.store
        order_id = str(payload["order_id"])
        marker = str(payload["marker"])
        try:
            order = store.get_order(order_id) if maybe_sent else None
            if order is None or not landed(order, marker):
                order = store.append_status(
                    order_id,
                    title=str(payload["title"]),
                    comment=str(payload["comment"]),
                    username=context.settings.konimbo_status_username,
                )
                if not landed(order, marker):
                    order = store.get_order(order_id)
        except ProviderError as error:
            self._failed_attempt(job_id, error)
            return
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            job = present(job)
            now = context.now()
            if not landed(order, marker):
                self._give_up_or_retry(job, "konimbo_unverified", now)
                self._notify(session, job)
                return
            if payload.get("changes_state") and order.status_title != payload["title"]:
                # Konimbo kept the line in the history but the order did not move: the name
                # is not one of the store's order states.
                job.last_error = "konimbo_status_unknown"
                finish(job, JobState.FAILED, now, {"status": order.status_title})
                self._notify(session, job)
                return
            finish(job, JobState.SUCCEEDED, now, {"status": order.status_title})
            self._record_status(session, job, order)
            self.completion.after_store_job(session, job)

    def _record_status(self, session: Session, job: Job, store_order: StoreOrder) -> None:
        if job.order_id is None:
            return
        order = session.get(Order, job.order_id)
        if order is not None and store_order.status_title:
            order.status_title = store_order.status_title

    def _failed_attempt(self, job_id: int, error: ProviderError) -> None:
        with self.context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            job = present(job)
            now = self.context.now()
            if error.code in ("konimbo_rejected", "konimbo_order_not_found"):
                job.last_error = error.code
                finish(job, JobState.FAILED, now)
            else:
                # Timeouts and outages: the next attempt reads the history back before sending.
                self._give_up_or_retry(job, error.code, now)
            self._notify(session, job)

    def _give_up_or_retry(self, job: Job, code: str, now: Any) -> None:
        if job.attempts < job.max_attempts:
            retry_later(job, code, now, backoff(job.attempts))
        else:
            job.last_error = code
            finish(job, JobState.FAILED, now)

    def _notify(self, session: Session, job: Job) -> None:
        if job.fulfillment_id is not None:
            fulfillment = get_fulfillment(session, job.fulfillment_id)
            changed(session, self.context.events, fulfillment, self.context.now())

    # Reading the shipment number back.

    def run_find(self, job_id: int) -> None:
        context = self.context
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None or job.state != JobState.RUNNING or job.fulfillment_id is None:
                return
            fulfillment = get_fulfillment(session, job.fulfillment_id)
            now = context.now()
            if (
                fulfillment.shipping_profile == ShippingProfile.NONE
                or usable_shipment(session, fulfillment.id) is not None
            ):
                finish(job, JobState.SUCCEEDED, now, {"found": "already"})
                return
            if context.providers.store is None:
                job.last_error = "konimbo_not_configured"
                finish(job, JobState.FAILED, now)
                return
            since = self._sent_at(session, fulfillment)
            box = fulfillment.shipping_profile == ShippingProfile.BOX
            excluded = self._excluded_numbers(session, fulfillment)
            order_id = str(job.payload["order_id"])
        try:
            order = context.providers.store.get_order(order_id)
        except ProviderError as error:
            with context.db.session(write=True) as session:
                job = session.get(Job, job_id, with_for_update=True)
                job = present(job)
                self._find_retry(job, error.code, context.now())
            return
        numbers = [
            number for number in self.numbers_in(order, since=since) if number not in excluded
        ]
        verified: tuple[int | None, Any] = (None, None)
        if len(numbers) == 1:
            try:
                verified = self.shipments.verify(numbers[0], box=box)
            except ServiceError as error:
                numbers = []
                logger.warning(
                    "A shipment number found in Konimbo was not accepted",
                    extra={"event": {"job": job_id, "code": error.code}},
                )
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            job = present(job)
            now = context.now()
            if not numbers:
                self._find_retry(job, "shipment_not_in_store", now)
                if job.state == JobState.FAILED:
                    self._notify(session, job)
                return
            if len(numbers) > 1:
                job.last_error = "shipment_number_ambiguous"
                finish(job, JobState.FAILED, now, {"count": len(numbers)})
                self._notify(session, job)
                return
            fulfillment = get_fulfillment(session, job.fulfillment_id or 0, lock=True)
            customer_code, status = verified
            try:
                self.shipments.attach(
                    session,
                    fulfillment,
                    numbers[0],
                    source=ShipmentSource.KONIMBO,
                    actor=SYSTEM_ACTOR,
                    customer_code=customer_code,
                    status_code=status.code if status else None,
                    status_text=status.text if status else None,
                )
            except ServiceError as error:
                job.last_error = error.code
                finish(job, JobState.FAILED, now)
                self._notify(session, job)
                return
            finish(job, JobState.SUCCEEDED, now, {"shipment": numbers[0]})

    def numbers_in(self, order: StoreOrder, *, since: Any) -> list[str]:
        """Shipment numbers in the order: Konimbo's own history lines and tracking fields."""
        config = self.context.config()
        texts: list[str] = []
        for status in order.statuses:
            if not status.comment or OWN_MARKER in status.comment:
                continue
            if since is not None and status.created_at is not None and status.created_at < since:
                continue
            texts.append(status.comment)
        texts.extend(order.references)
        found: list[str] = []
        for text in texts:
            for number in config.shipment_numbers(text):
                if number not in found and number != order.id:
                    found.append(number)
        return found

    def _sent_at(self, session: Session, fulfillment: Fulfillment) -> Any:
        """When the order was sent to Cargo, if this service sent it."""
        transmit = [
            job
            for job in jobs_with(session, fulfillment.id, PURPOSE_TRANSMIT)
            if job.state == JobState.SUCCEEDED and job.finished_at is not None
        ]
        if transmit and transmit[-1].finished_at is not None:
            return transmit[-1].finished_at - CLOCK_SKEW
        return None

    def _excluded_numbers(self, session: Session, fulfillment: Fulfillment) -> set[str]:
        """Numbers that must not be taken: cancelled or failed here, or an order's own number."""
        rows = session.scalars(
            select(Shipment.external_id).where(
                Shipment.fulfillment_id == fulfillment.id,
                Shipment.external_id.is_not(None),
                Shipment.state.not_in(list(USABLE_SHIPMENT_STATES)),
            )
        ).all()
        excluded = {row for row in rows if row}
        excluded.update(order.id for order in fulfillment.orders)
        return excluded

    def _find_retry(self, job: Job, code: str, now: Any) -> None:
        if job.attempts < job.max_attempts:
            retry_later(job, code, now, backoff(job.attempts, base=20, cap=300))
        else:
            job.last_error = code
            finish(job, JobState.FAILED, now)

    # A person asks for another try.

    def retry(self, actor: Principal, job_id: int) -> None:
        context = self.context
        with context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise ServiceError("job_not_found")
            retryable = job.kind in (
                JobKind.KONIMBO_STATUS,
                JobKind.KONIMBO_NOTE,
                JobKind.KONIMBO_FIND_SHIPMENT,
            ) and job.state in (JobState.FAILED, JobState.UNCERTAIN)
            if not retryable:
                raise ServiceError("job_not_retryable")
            now = context.now()
            job.state = JobState.PENDING
            job.attempts = 0
            job.run_after = now
            job.finished_at = None
            job.updated_at = now
            job.dry_run = job.dry_run and context.store_dry_run
            audit(
                session,
                "job.retried",
                actor=actor.id,
                now=now,
                fulfillment_id=job.fulfillment_id,
                job=job.id,
                kind=job.kind,
            )
            self._notify(session, job)


def is_find_job(job: Job) -> bool:
    return job.payload.get("purpose") == PURPOSE_FIND
