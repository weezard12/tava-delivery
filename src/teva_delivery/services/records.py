"""Small building blocks the services share: locking a packing unit, the logs, the outbox."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import ActionSource, EventKind, FulfillmentState, JobKind, JobState
from ..domain.errors import ServiceError
from ..domain.packing import check_transition
from ..persistence.database import after_commit
from ..persistence.models import AuditEvent, Fulfillment, Job, PackingEvent, Transition
from .events import EventBus


def present[T](value: T | None) -> T:
    """A row that must exist (it was read a moment ago in this unit of work)."""
    if value is None:
        raise ServiceError("internal_error")
    return value


def get_fulfillment(session: Session, fulfillment_id: int, *, lock: bool = False) -> Fulfillment:
    query = select(Fulfillment).where(Fulfillment.id == fulfillment_id)
    if lock:
        query = query.with_for_update()
    fulfillment = session.scalars(query).one_or_none()
    if fulfillment is None:
        raise ServiceError("fulfillment_not_found")
    return fulfillment


def held_by(fulfillment: Fulfillment, actor: str, now: datetime) -> bool:
    return (
        fulfillment.assigned_to == actor
        and fulfillment.lease_until is not None
        and fulfillment.lease_until > now
    )


def held_by_other(fulfillment: Fulfillment, actor: str, now: datetime) -> bool:
    return (
        fulfillment.assigned_to is not None
        and fulfillment.assigned_to != actor
        and fulfillment.lease_until is not None
        and fulfillment.lease_until > now
    )


def move(
    session: Session,
    fulfillment: Fulfillment,
    target: FulfillmentState,
    *,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> None:
    current = FulfillmentState(fulfillment.state)
    if current is target:
        return
    check_transition(current, target)
    fulfillment.state = target
    session.add(
        Transition(
            fulfillment_id=fulfillment.id,
            at=now,
            actor=actor,
            from_state=current,
            to_state=target,
            reason=reason,
        )
    )


def touch(fulfillment: Fulfillment, now: datetime) -> None:
    fulfillment.version += 1
    fulfillment.updated_at = now


def changed(session: Session, events: EventBus, fulfillment: Fulfillment, now: datetime) -> None:
    """Bump the version and tell open screens once the transaction commits."""
    touch(fulfillment, now)
    pending: dict[int, int] | None = session.info.get("changed_fulfillments")
    if pending is None:
        pending = {}
        session.info["changed_fulfillments"] = pending

        def publish() -> None:
            for fulfillment_id, version in pending.items():
                events.fulfillment_changed(fulfillment_id, version)
            events.queue_changed()

        after_commit(session, publish)
    pending[fulfillment.id] = fulfillment.version


def log_event(
    session: Session,
    fulfillment: Fulfillment,
    kind: EventKind,
    *,
    actor: str,
    now: datetime,
    source: ActionSource = ActionSource.MANUAL,
    line_id: int | None = None,
    note_id: int | None = None,
    quantity: int | None = None,
    reason: str | None = None,
    utterance_id: str | None = None,
    reverts_id: int | None = None,
) -> PackingEvent:
    entry = PackingEvent(
        fulfillment_id=fulfillment.id,
        at=now,
        actor=actor,
        kind=kind,
        source=source,
        line_id=line_id,
        note_id=note_id,
        quantity=quantity,
        reason=reason,
        utterance_id=utterance_id,
        reverts_id=reverts_id,
    )
    session.add(entry)
    session.flush()
    return entry


def audit(
    session: Session,
    action: str,
    *,
    actor: str,
    now: datetime,
    fulfillment_id: int | None = None,
    **details: Any,
) -> None:
    session.add(
        AuditEvent(
            at=now, actor=actor, action=action, fulfillment_id=fulfillment_id, details=details
        )
    )


def enqueue(
    session: Session,
    kind: JobKind,
    key: str,
    payload: dict[str, Any],
    *,
    now: datetime,
    max_attempts: int,
    fulfillment_id: int | None = None,
    order_id: str | None = None,
    delay: float = 0,
    dry_run: bool = False,
) -> Job:
    """Add a job unless one with this key exists already; either way return it."""
    existing = session.scalars(select(Job).where(Job.idempotency_key == key)).one_or_none()
    if existing is not None:
        return existing
    job = Job(
        kind=kind,
        idempotency_key=key,
        fulfillment_id=fulfillment_id,
        order_id=order_id,
        payload=payload,
        state=JobState.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        run_after=now + timedelta(seconds=delay),
        dry_run=dry_run,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def jobs_for(session: Session, fulfillment_id: int) -> list[Job]:
    return list(
        session.scalars(select(Job).where(Job.fulfillment_id == fulfillment_id).order_by(Job.id))
    )
