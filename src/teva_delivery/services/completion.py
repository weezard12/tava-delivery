"""Finishing a packed order: a valid shipment, its printed label, then ממתין לאיסוף in Konimbo.

``advance`` looks at the facts and starts the next external step. It is called after every
change that could unblock one (packing done, a shipment found or created, a label printed, a
job finished), so a restart or a failure never loses track of where an order stands, and no
step is started twice: each one is a job with a fixed idempotency key.

Two ways to get the Cargo shipment, chosen on the settings screen:

* ``konimbo`` (the default): Konimbo's own Cargo integration sends the order to Cargo when its
  status changes. The service sets that status (when packing starts or ends, or never if the
  office does it) and then reads the shipment number back from the Konimbo order.
* ``cargo``: the service creates the shipment through Cargo's API itself.
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import (
    ACTIVE_JOB_STATES,
    USABLE_SHIPMENT_STATES,
    FulfillmentState,
    JobKind,
    JobState,
    PrintState,
    ShipmentState,
    ShippingProfile,
)
from ..domain.errors import message_for
from ..domain.identity import SYSTEM_ACTOR
from ..persistence.models import Fulfillment, Job, Order, PrintJob, Shipment
from .context import Context
from .records import audit, changed, enqueue, jobs_for, move
from .views import StepView
from .warehouse_config import WarehouseConfig

PURPOSE_PICKUP = "awaiting_pickup"
PURPOSE_PREPARING = "preparing"
PURPOSE_TRANSMIT = "transmit"
PURPOSE_INVENTORY = "inventory"
PURPOSE_RESTORE = "restore"
PURPOSE_NOTE = "note"
PURPOSE_FIND = "find_shipment"
# Every comment this service writes to Konimbo carries a marker that starts with this.
OWN_MARKER = "[מחסן #"


def marker_for(key: str) -> str:
    """A short tag written into the Konimbo comment, so a retry can see the first one landed."""
    return f"{OWN_MARKER}{hashlib.sha256(key.encode()).hexdigest()[:10]}]"


def latest_shipment(session: Session, fulfillment_id: int) -> Shipment | None:
    return session.scalars(
        select(Shipment)
        .where(Shipment.fulfillment_id == fulfillment_id)
        .order_by(Shipment.id.desc())
        .limit(1)
    ).one_or_none()


def usable_shipment(session: Session, fulfillment_id: int) -> Shipment | None:
    shipment = latest_shipment(session, fulfillment_id)
    return shipment if shipment is not None and shipment.state in USABLE_SHIPMENT_STATES else None


def prints_for(session: Session, shipment_id: int) -> list[PrintJob]:
    return list(
        session.scalars(
            select(PrintJob).where(PrintJob.shipment_id == shipment_id).order_by(PrintJob.id)
        )
    )


def primary_order(fulfillment: Fulfillment) -> Order:
    return next(
        (order for order in fulfillment.orders if order.id == fulfillment.primary_order_id),
        fulfillment.orders[0],
    )


def store_job(
    session: Session,
    context: Context,
    fulfillment: Fulfillment,
    order_id: str,
    *,
    purpose: str,
    title: str,
    comment: str,
    key: str,
    kind: JobKind = JobKind.KONIMBO_STATUS,
    delay: float = 0,
) -> Job:
    marker = marker_for(key)
    return enqueue(
        session,
        kind,
        key,
        {
            "order_id": order_id,
            "title": title,
            "comment": f"{comment} {marker}".strip(),
            "marker": marker,
            "purpose": purpose,
            # A title that is one of the store's order states moves the order to that state.
            "changes_state": kind is JobKind.KONIMBO_STATUS,
        },
        now=context.now(),
        max_attempts=context.settings.job_max_attempts,
        fulfillment_id=fulfillment.id,
        order_id=order_id,
        delay=delay,
        dry_run=context.store_dry_run,
    )


def jobs_with(session: Session, fulfillment_id: int, purpose: str) -> list[Job]:
    return [
        job for job in jobs_for(session, fulfillment_id) if job.payload.get("purpose") == purpose
    ]


class Completion:
    def __init__(self, context: Context) -> None:
        self.context = context

    # Facts.

    def needs_shipment(self, fulfillment: Fulfillment) -> bool:
        return fulfillment.shipping_profile != ShippingProfile.NONE

    def label_required(self, fulfillment: Fulfillment, config: WarehouseConfig) -> bool:
        """Whether completion waits for the printed label.

        Only when labels can be printed here at all, and not when the shipment can only exist
        after completion (the awaiting pickup status is the one that sends it to Cargo).
        """
        return (
            self.needs_shipment(fulfillment)
            and config.require_label_print
            and self.context.can_print_labels
            and not (config.shipment_source == "konimbo" and config.transmits_at_pickup)
        )

    def label_done(self, session: Session, shipment: Shipment) -> bool:
        return any(job.state == PrintState.ACCEPTED for job in prints_for(session, shipment.id))

    # Steps started by packing.

    def on_claim(self, session: Session, fulfillment: Fulfillment) -> None:
        """Packing started: tell Konimbo, and send to Cargo now if that is the setting."""
        config = self.context.config()
        if self._transmit_now(session, fulfillment, config, "claim"):
            # The transmit status also tells the office the warehouse has the order.
            fulfillment.preparing_sent = True
            return
        if config.status_preparing and not fulfillment.preparing_sent:
            store_job(
                session,
                self.context,
                fulfillment,
                fulfillment.primary_order_id,
                purpose=PURPOSE_PREPARING,
                title=config.status_preparing,
                comment="המחסן התחיל לארוז את ההזמנה.",
                key=f"preparing:{fulfillment.id}",
            )
            fulfillment.preparing_sent = True

    def _transmit_now(
        self, session: Session, fulfillment: Fulfillment, config: WarehouseConfig, moment: str
    ) -> bool:
        title = config.transmit_status
        if (
            config.shipment_source != "konimbo"
            or title is None
            or config.transmit_on != moment
            or not self.needs_shipment(fulfillment)
            or usable_shipment(session, fulfillment.id) is not None
        ):
            return False
        # Only the primary order is sent: orders packed together leave in one box.
        store_job(
            session,
            self.context,
            fulfillment,
            fulfillment.primary_order_id,
            purpose=PURPOSE_TRANSMIT,
            title=title,
            comment="נשלח ל-Cargo מהמחסן.",
            key=f"transmit:{fulfillment.id}",
        )
        return True

    def ensure_find_job(self, session: Session, fulfillment: Fulfillment) -> Job | None:
        """Start looking for the shipment number in the Konimbo order, once."""
        if not self.needs_shipment(fulfillment) or self.context.providers.store is None:
            return None
        if usable_shipment(session, fulfillment.id) is not None:
            return None
        finds = jobs_with(session, fulfillment.id, PURPOSE_FIND)
        if finds:
            # Running, done, or given up (a person then retries it or types the number in).
            return finds[-1]
        return enqueue(
            session,
            JobKind.KONIMBO_FIND_SHIPMENT,
            f"find:{fulfillment.id}:1",
            {"purpose": PURPOSE_FIND, "order_id": fulfillment.primary_order_id},
            now=self.context.now(),
            max_attempts=FIND_ATTEMPTS,
            fulfillment_id=fulfillment.id,
            order_id=fulfillment.primary_order_id,
            # Konimbo's integration needs a moment after the status change.
            delay=FIND_FIRST_DELAY,
        )

    def after_store_job(self, session: Session, job: Job) -> None:
        """A Konimbo write landed: start whatever was waiting on it."""
        if job.fulfillment_id is None:
            return
        from .records import get_fulfillment

        fulfillment = get_fulfillment(session, job.fulfillment_id, lock=True)
        purpose = job.payload.get("purpose")
        config = self.context.config()
        if not job.dry_run and (
            purpose == PURPOSE_TRANSMIT
            or (purpose == PURPOSE_PICKUP and config.shipment_source == "konimbo")
        ):
            self.ensure_find_job(session, fulfillment)
        self.advance(session, fulfillment)
        changed(session, self.context.events, fulfillment, self.context.now())

    # The completion pipeline.

    def advance(self, session: Session, fulfillment: Fulfillment) -> bool:
        """Start the next step of a packed order. Returns whether anything changed."""
        if fulfillment.state != FulfillmentState.PACKED:
            return False
        context = self.context
        config = context.config()
        shipment_number = None
        if self.needs_shipment(fulfillment):
            waiting, started = self._shipment_ready(session, fulfillment, config)
            if waiting:
                return started
            shipment = usable_shipment(session, fulfillment.id)
            shipment_number = shipment.external_id if shipment is not None else None

        orders = fulfillment.orders
        jobs = {job.order_id: job for job in jobs_with(session, fulfillment.id, PURPOSE_PICKUP)}
        started = False
        for order in orders:
            if order.id in jobs:
                continue
            comment = "ההזמנה נארזה במחסן ומוכנה לאיסוף."
            if shipment_number:
                comment += f" משלוח Cargo {shipment_number}."
            if len(orders) > 1:
                comment += " נשלחת יחד עם " + ", ".join(
                    f"#{other.id}" for other in orders if other.id != order.id
                )
            store_job(
                session,
                context,
                fulfillment,
                order.id,
                purpose=PURPOSE_PICKUP,
                title=config.status_awaiting_pickup,
                comment=comment,
                key=f"pickup:{fulfillment.id}:{order.id}",
            )
            started = True
        if started:
            return True
        pickup = jobs_with(session, fulfillment.id, PURPOSE_PICKUP)
        if pickup and all(job.state == JobState.SUCCEEDED for job in pickup):
            now = context.now()
            move(
                session,
                fulfillment,
                FulfillmentState.COMPLETED,
                actor=SYSTEM_ACTOR,
                now=now,
                reason="store_confirmed",
            )
            fulfillment.completed_at = now
            fulfillment.assigned_to = None
            fulfillment.lease_until = None
            audit(
                session,
                "fulfillment.completed",
                actor=SYSTEM_ACTOR,
                now=now,
                fulfillment_id=fulfillment.id,
                dry_run=any(job.dry_run for job in pickup),
            )
            return True
        return False

    def _shipment_ready(
        self, session: Session, fulfillment: Fulfillment, config: WarehouseConfig
    ) -> tuple[bool, bool]:
        """(still waiting, started something) for the shipment and label steps."""
        shipment = usable_shipment(session, fulfillment.id)
        label = self.label_required(fulfillment, config)
        if shipment is None:
            if config.shipment_source == "cargo":
                from .shipments import request_shipment

                if latest_shipment(session, fulfillment.id) is None and (
                    self.context.can_create_shipments
                ):
                    request_shipment(session, self.context, fulfillment, actor=SYSTEM_ACTOR)
                    return True, True
                # Cargo mode always needs the shipment; a person links one if it cannot be made.
                return True, False
            transmit = jobs_with(session, fulfillment.id, PURPOSE_TRANSMIT)
            started = False
            if not transmit:
                started = self._transmit_now(session, fulfillment, config, "complete")
                transmit = jobs_with(session, fulfillment.id, PURPOSE_TRANSMIT)
            last = transmit[-1] if transmit else None
            if last is not None and last.state != JobState.SUCCEEDED:
                # The order is sent to Cargo before it is marked as waiting for pickup.
                return True, started
            if last is not None and last.dry_run:
                # Test mode: nothing went to Cargo, so there is no number to wait for.
                return False, started
            if not config.transmits_at_pickup:
                had = bool(jobs_with(session, fulfillment.id, PURPOSE_FIND))
                self.ensure_find_job(session, fulfillment)
                started = started or not had
            return label, started
        if label and not self.label_done(session, shipment):
            return True, False
        return False, False

    # What the screen shows.

    def steps(self, session: Session, fulfillment: Fulfillment) -> list[StepView]:
        config = self.context.config()
        steps: list[StepView] = []
        state = FulfillmentState(fulfillment.state)
        finishing = state in (FulfillmentState.PACKED, FulfillmentState.COMPLETED)
        shipment = latest_shipment(session, fulfillment.id)
        if not self.needs_shipment(fulfillment):
            steps.append(StepView(key="shipment", state="skipped", message="ללא משלוח Cargo"))
            steps.append(StepView(key="label", state="skipped"))
        else:
            steps.append(self._shipment_step(session, fulfillment, shipment, finishing, config))
            usable = shipment if shipment and shipment.state in USABLE_SHIPMENT_STATES else None
            steps.append(self._label_step(session, fulfillment, usable, config))
        steps.append(self._store_step(session, fulfillment, config))
        return steps

    def _store_step(
        self, session: Session, fulfillment: Fulfillment, config: WarehouseConfig
    ) -> StepView:
        pickup = jobs_with(session, fulfillment.id, PURPOSE_PICKUP)
        if not pickup:
            return StepView(
                key="store_status",
                state="waiting",
                message=f"יעודכן ל{config.status_awaiting_pickup} בסיום",
            )
        states = {JobState(job.state) for job in pickup}
        dry = any(job.dry_run for job in pickup)
        if states == {JobState.SUCCEEDED}:
            return StepView(
                key="store_status",
                state="done",
                dry_run=dry,
                message=(
                    "מצב בדיקה: העדכון לקונימבו נרשם ולא נשלח"
                    if dry
                    else f"עודכן בקונימבו ל{config.status_awaiting_pickup}"
                ),
            )
        failed = [job for job in pickup if job.state in (JobState.FAILED, JobState.UNCERTAIN)]
        if failed:
            code = failed[0].last_error or "konimbo_unavailable"
            return StepView(
                key="store_status", state="failed", code=code, message=message_for(code)
            )
        return StepView(key="store_status", state="running", message="מעדכן את קונימבו")

    def _shipment_step(
        self,
        session: Session,
        fulfillment: Fulfillment,
        shipment: Shipment | None,
        finishing: bool,
        config: WarehouseConfig,
    ) -> StepView:
        if shipment is None:
            if config.shipment_source == "konimbo":
                return self._konimbo_shipment_step(session, fulfillment, finishing, config)
            return self._cargo_pending_step(finishing)
        match ShipmentState(shipment.state):
            case ShipmentState.CREATED | ShipmentState.LINKED:
                return StepView(
                    key="shipment", state="done", message=f"משלוח Cargo {shipment.external_id}"
                )
            case ShipmentState.REQUESTED:
                return StepView(key="shipment", state="running", message="יוצר משלוח ב-Cargo")
            case ShipmentState.UNCERTAIN:
                return StepView(
                    key="shipment",
                    state="uncertain",
                    code="shipment_uncertain",
                    message=message_for("shipment_uncertain"),
                )
            case ShipmentState.CANCELLED:
                return StepView(
                    key="shipment",
                    state="failed",
                    code="shipment_cancelled",
                    message=message_for("shipment_cancelled"),
                )
            case _:
                code = shipment.error_code or "cargo_rejected"
                return StepView(
                    key="shipment", state="failed", code=code, message=message_for(code)
                )

    def _cargo_pending_step(self, finishing: bool) -> StepView:
        context = self.context
        if not context.can_create_shipments:
            code = (
                "cargo_not_configured"
                if context.providers.carrier is None
                else "cargo_writes_disabled"
            )
            return StepView(key="shipment", state="blocked", code=code, message=message_for(code))
        return StepView(
            key="shipment",
            state="pending" if finishing else "waiting",
            message="המשלוח ייווצר ב-Cargo בסיום האריזה",
        )

    def _konimbo_shipment_step(
        self,
        session: Session,
        fulfillment: Fulfillment,
        finishing: bool,
        config: WarehouseConfig,
    ) -> StepView:
        transmit = jobs_with(session, fulfillment.id, PURPOSE_TRANSMIT)
        finds = jobs_with(session, fulfillment.id, PURPOSE_FIND)
        if transmit and transmit[-1].state in (JobState.FAILED, JobState.UNCERTAIN):
            code = transmit[-1].last_error or "konimbo_unavailable"
            return StepView(key="shipment", state="failed", code=code, message=message_for(code))
        if finds:
            last = finds[-1]
            if last.state in ACTIVE_JOB_STATES:
                return StepView(
                    key="shipment",
                    state="running",
                    message="מחכה שמספר המשלוח של Cargo יופיע בקונימבו",
                )
            if last.state == JobState.FAILED:
                code = last.last_error or "shipment_not_in_store"
                return StepView(
                    key="shipment", state="failed", code=code, message=message_for(code)
                )
        if transmit and transmit[-1].state in ACTIVE_JOB_STATES:
            return StepView(key="shipment", state="running", message="שולח את ההזמנה ל-Cargo")
        if config.transmits_at_pickup:
            return StepView(
                key="shipment",
                state="waiting",
                message=f"יישלח ל-Cargo כשההזמנה תעבור ל{config.status_awaiting_pickup}",
            )
        if config.transmit_status is None and config.transmit_on != "off":
            return StepView(
                key="shipment",
                state="blocked",
                code="transmit_not_configured",
                message=message_for("transmit_not_configured"),
            )
        if config.transmit_on == "off":
            return StepView(
                key="shipment",
                state="pending" if finishing else "waiting",
                message="המשרד שולח ל-Cargo; מספר המשלוח ייקרא מקונימבו",
            )
        moment = "בתחילת האריזה" if config.transmit_on == "claim" else "בסיום האריזה"
        return StepView(
            key="shipment", state="waiting", message=f"יישלח ל-Cargo דרך קונימבו {moment}"
        )

    def _label_step(
        self,
        session: Session,
        fulfillment: Fulfillment,
        shipment: Shipment | None,
        config: WarehouseConfig,
    ) -> StepView:
        if not self.context.can_print_labels:
            code = (
                "label_printing_unavailable"
                if self.context.providers.carrier is None
                else "printer_not_configured"
            )
            return StepView(key="label", state="skipped", code=code, message=message_for(code))
        prints = prints_for(session, shipment.id) if shipment is not None else []
        if any(job.state == PrintState.ACCEPTED for job in prints):
            return StepView(key="label", state="done", message="המדבקה הודפסה")
        if prints:
            last = prints[-1]
            code = last.error_code or (
                "print_outcome_unknown" if last.state == PrintState.UNCERTAIN else "print_failed"
            )
            return StepView(
                key="label",
                state="uncertain" if last.state == PrintState.UNCERTAIN else "failed",
                code=code,
                message=message_for(code),
            )
        if not self.label_required(fulfillment, config):
            if shipment is None:
                return StepView(key="label", state="skipped", message="הדפסה לפי בקשה")
            return StepView(key="label", state="pending", message="אפשר להדפיס את המדבקה")
        if shipment is None:
            return StepView(key="label", state="waiting", message="המדבקה תודפס כשהמשלוח מוכן")
        return StepView(key="label", state="pending", message='אמרו "תדפיס מדבקה" או לחצו הדפסה')


# Looking for the shipment number: first look soon after the status change, then back off.
FIND_FIRST_DELAY = 20
FIND_ATTEMPTS = 20
