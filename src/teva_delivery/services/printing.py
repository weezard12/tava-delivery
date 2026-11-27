"""Printing the official Cargo label through teva-printer.

The label is always Cargo's own PDF, fetched by its link for the shipment of this order; this
service never draws a label or a barcode. Before anything is printed the link is checked to
be a Cargo label for exactly this shipment number. A print is recorded as ``uncertain``
before it is sent, so a print whose answer was lost is never repeated without a person
checking the printer first. The employee's workspace session is passed on to teva-printer,
so the printer records who printed.
"""

import logging
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import (
    Action,
    FulfillmentState,
    PrintMode,
    PrintState,
    ShipmentState,
)
from ..domain.errors import ProviderError, ServiceError
from ..domain.identity import Principal
from ..domain.ports import PrintCredentials
from ..persistence.models import Fulfillment, PrintJob
from .completion import Completion, latest_shipment, prints_for, usable_shipment
from .context import Context
from .presenter import Presenter
from .records import audit, changed, get_fulfillment, held_by_other, present
from .views import ActionResult

logger = logging.getLogger("teva_delivery.printing")

CARGO_LABEL_HOST = "api-v2.cargo.co.il"
CARGO_LABEL_PATH = "/labels/"


def label_matches(url: str, shipment_number: str) -> bool:
    """Cargo's label link names the shipment: ``.../labels/<date>/shipment_<number>_...pdf``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname != CARGO_LABEL_HOST or parts.port is not None:
        return False
    if parts.username or parts.password or not parts.path.startswith(CARGO_LABEL_PATH):
        return False
    name = parts.path.rsplit("/", 1)[-1]
    return name.startswith(f"shipment_{shipment_number}_") and name.endswith(".pdf")


class Printing:
    def __init__(self, context: Context, completion: Completion, presenter: Presenter) -> None:
        self.context = context
        self.completion = completion
        self.presenter = presenter

    def print_label(
        self,
        actor: Principal,
        fulfillment_id: int,
        *,
        mode: PrintMode,
        credentials: PrintCredentials,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        context = self.context
        carrier = context.providers.carrier
        printer = context.providers.printer
        config = context.config()
        if carrier is None:
            raise ServiceError("label_printing_unavailable")
        if printer is None or not config.label_printer_id:
            raise ServiceError("printer_not_configured")
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            self._check_state(
                fulfillment.state, held_by_other(fulfillment, actor.id, context.now())
            )
            shipment = usable_shipment(session, fulfillment.id)
            if shipment is None or shipment.external_id is None:
                latest = latest_shipment(session, fulfillment.id)
                if latest is not None and latest.state == ShipmentState.REQUESTED:
                    raise ServiceError("shipment_pending")
                if latest is not None and latest.state == ShipmentState.UNCERTAIN:
                    raise ServiceError("shipment_uncertain")
                raise ServiceError("shipment_missing")
            prints = prints_for(session, shipment.id)
            if idempotency_key:
                earlier = session.scalars(
                    select(PrintJob).where(PrintJob.idempotency_key == idempotency_key)
                ).one_or_none()
                if earlier is not None:
                    return self._result(session, fulfillment, actor, earlier)
            if mode is not PrintMode.REPRINT:
                if any(job.state == PrintState.ACCEPTED for job in prints):
                    raise ServiceError("already_printed")
                if any(job.state == PrintState.UNCERTAIN for job in prints):
                    raise ServiceError("print_outcome_unknown")
                if mode is PrintMode.AUTO and prints:
                    raise ServiceError("already_printed")
            now = context.now()
            job = PrintJob(
                fulfillment_id=fulfillment.id,
                shipment_id=shipment.id,
                idempotency_key=idempotency_key or f"print:{shipment.id}:{len(prints) + 1}",
                mode=mode,
                printer_id=config.label_printer_id,
                # On record before it is sent; an answer that never comes leaves it uncertain.
                state=PrintState.UNCERTAIN,
                requested_by=actor.id,
                created_at=now,
            )
            session.add(job)
            session.flush()
            job_id = job.id
            number = shipment.external_id
        try:
            url = carrier.label_url(number)
        except ProviderError as error:
            return self._finish(actor, job_id, PrintState.FAILED, error_code=error.code)
        if not label_matches(url, number):
            logger.error(
                "Cargo returned a label that does not name the shipment",
                extra={"event": {"print_job": job_id}},
            )
            return self._finish(actor, job_id, PrintState.FAILED, error_code="label_mismatch")
        try:
            receipt = printer.print_label(
                url,
                printer_id=config.label_printer_id,
                direct=context.settings.print_direct,
                credentials=credentials,
            )
        except ProviderError as error:
            state = PrintState.UNCERTAIN if error.sent else PrintState.FAILED
            code = "print_outcome_unknown" if error.sent else error.code
            return self._finish(actor, job_id, state, error_code=code)
        warning = receipt.warning
        if receipt.shipment and receipt.shipment != number:
            warning = "label_mismatch"
        return self._finish(
            actor,
            job_id,
            PrintState.ACCEPTED,
            printer_job_id=receipt.job_id,
            warning=warning,
        )

    def _check_state(self, state: str, locked: bool) -> None:
        if state in (FulfillmentState.CANCELLED, FulfillmentState.REVIEW):
            raise ServiceError("invalid_state")
        if state == FulfillmentState.PACKING and locked:
            raise ServiceError("fulfillment_locked")

    def _finish(
        self,
        actor: Principal,
        job_id: int,
        state: PrintState,
        *,
        error_code: str | None = None,
        printer_job_id: str | None = None,
        warning: str | None = None,
    ) -> ActionResult:
        context = self.context
        with context.db.session(write=True) as session:
            job = session.get(PrintJob, job_id, with_for_update=True)
            job = present(job)
            fulfillment = get_fulfillment(session, job.fulfillment_id, lock=True)
            job.state = state
            job.error_code = error_code
            job.printer_job_id = printer_job_id
            job.warning = warning
            now = context.now()
            audit(
                session,
                "label.print",
                actor=actor.id,
                now=now,
                fulfillment_id=fulfillment.id,
                mode=job.mode,
                state=str(state),
                code=error_code,
            )
            if state is PrintState.ACCEPTED:
                self.completion.advance(session, fulfillment)
            changed(session, context.events, fulfillment, now)
            session.flush()
            result = (
                self._result(session, fulfillment, actor, job)
                if state is PrintState.ACCEPTED
                else None
            )
        # The failure is on record (committed) before the employee hears about it.
        if result is None:
            raise ServiceError(error_code or "print_failed")
        return result

    def _result(
        self, session: Session, fulfillment: Fulfillment, actor: Principal, job: PrintJob
    ) -> ActionResult:
        if job.state != PrintState.ACCEPTED:
            raise ServiceError(job.error_code or "print_failed")
        speech = "המדבקה נשלחה למדפסת."
        if job.warning == "label_mismatch":
            speech = "המדבקה הודפסה, אבל המדפסת זיהתה מספר משלוח אחר. בדקו את המדבקה."
        return ActionResult(
            ok=True,
            action=Action.PRINT_SHIPPING_LABEL,
            message=speech,
            speech=speech,
            data={"print_job": job.id, "warning": job.warning},
            fulfillment=self.presenter.fulfillment(session, fulfillment, actor),
        )
