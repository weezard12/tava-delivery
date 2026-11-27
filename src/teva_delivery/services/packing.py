"""The packing actions. Every change to an order's progress goes through ``Packing.execute``.

The screen buttons and the voice assistant ask for the same validated actions. An action
that changes progress needs the order to be open (packing) and held by the employee asking,
so two employees never pack the same order. Each change is a packing event, from which undo
works. A request with an idempotency key is answered once: a repeat gets the first answer.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import packing as rules
from ..domain.enums import (
    Action,
    ActionSource,
    EventKind,
    FulfillmentState,
    NoteKind,
    PrintMode,
)
from ..domain.errors import ServiceError
from ..domain.identity import Principal
from ..persistence.models import ActionRequest, Fulfillment, Line, Note, PackingEvent
from .completion import PURPOSE_PICKUP, Completion, jobs_with
from .context import Context
from .presenter import Presenter, active_lines, line_facts, progress_of
from .records import changed, get_fulfillment, held_by_other, log_event, move
from .views import ActionResult

# Actions that change an order's progress, and need it open and held by the employee.
CHANGING = frozenset(
    {
        Action.CONFIRM_PRODUCT,
        Action.UNDO_PRODUCT_CONFIRMATION,
        Action.CONFIRM_ORDER_NOTE,
        Action.UNCONFIRM_ORDER_NOTE,
        Action.SET_NOTE_KIND,
        Action.REPORT_MISSING_INVENTORY,
        Action.UNDO_MISSING_INVENTORY,
        Action.COMPLETE_ORDER,
    }
)
READING = frozenset({Action.GET_REMAINING_PRODUCTS, Action.READ_ORDER_NOTES})
UNDOABLE = (EventKind.LINE_CONFIRMED, EventKind.NOTE_CONFIRMED, EventKind.MISSING_REPORTED)
ACTION_REQUEST_DAYS = 2


@dataclass(frozen=True, slots=True)
class ActionInput:
    action: Action
    line_id: int | None = None
    note_id: int | None = None
    quantity: int | None = None
    everything: bool = False
    reason: str | None = None
    note_kind: NoteKind | None = None
    print_mode: PrintMode | None = None
    source: ActionSource = ActionSource.MANUAL
    utterance_id: str | None = None
    idempotency_key: str | None = None

    def fingerprint(self, fulfillment_id: int) -> str:
        data = asdict(self)
        data.pop("idempotency_key")
        data.pop("utterance_id")
        data["fulfillment_id"] = fulfillment_id
        text = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(text.encode()).hexdigest()


class Outcome(BaseModel):
    """What an action did, before the fresh view of the order is attached."""

    message: str
    speech: str
    data: dict[str, Any] = {}


def amount(value: int, title: str) -> str:
    return title if value == 1 else f"{value} {title}"


def remaining_speech(fulfillment: Fulfillment) -> str:
    lines = [line for line in active_lines(fulfillment) if line_facts(line).remaining > 0]
    notes = [
        note
        for note in fulfillment.notes
        if note.kind == NoteKind.PACKING and note.confirmed_at is None
    ]
    if not lines and not notes:
        return "הכול אושר. אפשר לסיים את ההזמנה."
    parts = []
    for line in lines:
        if line.missing:
            parts.append(f"{line.title} חסר במלאי")
        else:
            parts.append(amount(line_facts(line).remaining, line.title))
    if notes:
        parts.append(
            "הוראת אריזה: " + notes[0].text
            if len(notes) == 1
            else f"{len(notes)} הוראות אריזה פתוחות"
        )
    return "נשאר: " + ", ".join(parts)


class Packing:
    def __init__(self, context: Context, completion: Completion, presenter: Presenter) -> None:
        self.context = context
        self.completion = completion
        self.presenter = presenter

    def execute(self, actor: Principal, fulfillment_id: int, request: ActionInput) -> ActionResult:
        context = self.context
        with context.db.session(write=True) as session:
            if request.idempotency_key:
                previous = session.get(ActionRequest, request.idempotency_key)
                if previous is not None:
                    if previous.actor != actor.id or previous.fingerprint != request.fingerprint(
                        fulfillment_id
                    ):
                        raise ServiceError("idempotency_conflict")
                    fulfillment = get_fulfillment(session, fulfillment_id)
                    replay = ActionResult.model_validate(previous.response)
                    replay.fulfillment = self.presenter.fulfillment(session, fulfillment, actor)
                    return replay
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            outcome = self._apply(session, actor, fulfillment, request)
            result = ActionResult(
                ok=True,
                action=request.action,
                message=outcome.message,
                speech=outcome.speech,
                data=outcome.data,
            )
            if request.idempotency_key:
                session.add(
                    ActionRequest(
                        key=request.idempotency_key,
                        actor=actor.id,
                        fulfillment_id=fulfillment_id,
                        action=request.action,
                        fingerprint=request.fingerprint(fulfillment_id),
                        response=result.model_dump(mode="json"),
                        created_at=context.now(),
                    )
                )
            session.flush()
            result.fulfillment = self.presenter.fulfillment(session, fulfillment, actor)
            return result

    # Dispatch.

    def _apply(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        action = request.action
        if action in READING:
            return self._read(fulfillment, action)
        if action not in CHANGING:
            raise ServiceError("action_unknown")
        now = self.context.now()
        self._check_holder(fulfillment, actor)
        fulfillment.lease_until = now + timedelta(seconds=self.context.settings.lease_seconds)
        handler = {
            Action.CONFIRM_PRODUCT: self._confirm_product,
            Action.UNDO_PRODUCT_CONFIRMATION: self._undo,
            Action.CONFIRM_ORDER_NOTE: self._confirm_note,
            Action.UNCONFIRM_ORDER_NOTE: self._unconfirm_note,
            Action.SET_NOTE_KIND: self._set_note_kind,
            Action.REPORT_MISSING_INVENTORY: self._report_missing,
            Action.UNDO_MISSING_INVENTORY: self._undo_missing,
            Action.COMPLETE_ORDER: self._complete,
        }[action]
        outcome = handler(session, actor, fulfillment, request)
        changed(session, self.context.events, fulfillment, now)
        return outcome

    def _check_holder(self, fulfillment: Fulfillment, actor: Principal) -> None:
        state = FulfillmentState(fulfillment.state)
        now = self.context.now()
        if state is FulfillmentState.PACKED:
            raise ServiceError("already_packed")
        if state is FulfillmentState.COMPLETED:
            raise ServiceError("order_finished")
        if state is not FulfillmentState.PACKING:
            raise ServiceError("not_claimed")
        if held_by_other(fulfillment, actor.id, now):
            raise ServiceError("fulfillment_locked")
        if fulfillment.assigned_to != actor.id:
            # A lease that ran out with nobody else taking the order stays the employee's.
            raise ServiceError("not_claimed")

    # Reading.

    def _read(self, fulfillment: Fulfillment, action: Action) -> Outcome:
        if action is Action.GET_REMAINING_PRODUCTS:
            speech = remaining_speech(fulfillment)
            return Outcome(message=speech, speech=speech)
        notes = [note for note in fulfillment.notes if note.kind != NoteKind.INFO]
        if not notes:
            speech = "אין הערות להזמנה הזו."
        else:
            parts = []
            for note in notes:
                prefix = "לאריזה" if note.kind == NoteKind.PACKING else "לשליח"
                done = " (בוצע)" if note.confirmed_at is not None else ""
                parts.append(f"{prefix}: {note.text}{done}")
            speech = ". ".join(parts)
        return Outcome(message=speech, speech=speech)

    # Products.

    def _line(self, fulfillment: Fulfillment, line_id: int | None) -> Line:
        line = next((line for line in fulfillment.lines if line.id == line_id), None)
        if line is None:
            raise ServiceError("line_not_found")
        if line.removed:
            raise ServiceError("line_removed")
        return line

    def _confirm_product(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        line = self._line(fulfillment, request.line_id)
        facts = line_facts(line)
        if facts.remaining == 0:
            raise ServiceError("line_already_confirmed")
        quantity = facts.remaining if request.everything else (request.quantity or 1)
        rules.check_quantity(facts, quantity)
        line.confirmed += quantity
        self._log(
            session,
            actor,
            fulfillment,
            request,
            EventKind.LINE_CONFIRMED,
            line_id=line.id,
            quantity=quantity,
        )
        left = line_facts(line).remaining
        speech = f"אישרתי {amount(quantity, line.title)}."
        if left:
            speech += f" נשארו עוד {left}."
        if progress_of(fulfillment).complete:
            speech += " כל המוצרים וההוראות אושרו."
        return Outcome(
            message=speech, speech=speech, data={"line_id": line.id, "quantity": quantity}
        )

    def _undo(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        query = select(PackingEvent).where(
            PackingEvent.fulfillment_id == fulfillment.id,
            PackingEvent.reverted.is_(False),
        )
        if request.line_id is not None:
            query = query.where(
                PackingEvent.kind == EventKind.LINE_CONFIRMED,
                PackingEvent.line_id == request.line_id,
            )
        else:
            query = query.where(PackingEvent.kind.in_(list(UNDOABLE)))
        event = session.scalars(query.order_by(PackingEvent.id.desc()).limit(1)).first()
        if event is None:
            raise ServiceError("nothing_to_undo")
        event.reverted = True
        match EventKind(event.kind):
            case EventKind.LINE_CONFIRMED:
                line = self._line(fulfillment, event.line_id)
                line.confirmed = max(line.confirmed - (event.quantity or 0), 0)
                self._log(
                    session,
                    actor,
                    fulfillment,
                    request,
                    EventKind.LINE_UNCONFIRMED,
                    line_id=line.id,
                    quantity=event.quantity,
                    reverts_id=event.id,
                )
                speech = f"ביטלתי את האישור של {amount(event.quantity or 1, line.title)}."
                data: dict[str, Any] = {"line_id": line.id}
            case EventKind.NOTE_CONFIRMED:
                note = self._note(fulfillment, event.note_id)
                note.confirmed_at = None
                note.confirmed_by = None
                self._log(
                    session,
                    actor,
                    fulfillment,
                    request,
                    EventKind.NOTE_UNCONFIRMED,
                    note_id=note.id,
                    reverts_id=event.id,
                )
                speech = "ביטלתי את האישור של ההוראה."
                data = {"note_id": note.id}
            case _:
                line = self._line(fulfillment, event.line_id)
                line.missing = 0
                line.missing_reason = None
                self._log(
                    session,
                    actor,
                    fulfillment,
                    request,
                    EventKind.MISSING_CLEARED,
                    line_id=line.id,
                    reverts_id=event.id,
                )
                speech = f"ביטלתי את הדיווח שחסר {line.title}."
                data = {"line_id": line.id}
        return Outcome(message=speech, speech=speech, data=data)

    # Notes.

    def _note(self, fulfillment: Fulfillment, note_id: int | None) -> Note:
        note = next((note for note in fulfillment.notes if note.id == note_id), None)
        if note is None:
            raise ServiceError("note_not_found")
        return note

    def _confirm_note(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        if request.note_id is None:
            open_notes = [
                note
                for note in fulfillment.notes
                if note.kind == NoteKind.PACKING and note.confirmed_at is None
            ]
            if not open_notes:
                raise ServiceError("no_open_notes")
            if len(open_notes) > 1:
                raise ServiceError("note_required")
            note = open_notes[0]
        else:
            note = self._note(fulfillment, request.note_id)
        if note.confirmed_at is None:
            note.confirmed_at = self.context.now()
            note.confirmed_by = actor.id
            self._log(
                session, actor, fulfillment, request, EventKind.NOTE_CONFIRMED, note_id=note.id
            )
        speech = f"אישרתי: {note.text}."
        if progress_of(fulfillment).complete:
            speech += " כל המוצרים וההוראות אושרו."
        return Outcome(message=speech, speech=speech, data={"note_id": note.id})

    def _unconfirm_note(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        note = self._note(fulfillment, request.note_id)
        if note.confirmed_at is None:
            raise ServiceError("nothing_to_undo")
        note.confirmed_at = None
        note.confirmed_by = None
        self._log(session, actor, fulfillment, request, EventKind.NOTE_UNCONFIRMED, note_id=note.id)
        return Outcome(
            message="האישור בוטל.", speech="ביטלתי את האישור של ההוראה.", data={"note_id": note.id}
        )

    def _set_note_kind(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        note = self._note(fulfillment, request.note_id)
        if request.note_kind is None:
            raise ServiceError("action_unknown")
        note.kind = request.note_kind
        self._log(
            session,
            actor,
            fulfillment,
            request,
            EventKind.NOTE_KIND_CHANGED,
            note_id=note.id,
            reason=str(request.note_kind),
        )
        labels = {
            NoteKind.PACKING: "הוראת אריזה",
            NoteKind.DELIVERY: "הוראה לשליח",
            NoteKind.INFO: "מידע בלבד",
        }
        message = f"ההערה סומנה כ{labels[request.note_kind]}."
        return Outcome(message=message, speech=message, data={"note_id": note.id})

    # Missing inventory.

    def _report_missing(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        line = self._line(fulfillment, request.line_id)
        facts = line_facts(line)
        missing = request.quantity or facts.remaining or line.required
        if missing <= 0 or missing > line.required:
            raise ServiceError("quantity_invalid")
        reason = (request.reason or "").strip()[:500] or None
        line.missing = missing
        line.missing_reason = reason
        self._log(
            session,
            actor,
            fulfillment,
            request,
            EventKind.MISSING_REPORTED,
            line_id=line.id,
            quantity=missing,
            reason=reason,
        )
        speech = f"רשמתי שחסר {line.title}. כשתעברו להזמנה הבאה, ההזמנה תעבור לממתין למלאי."
        return Outcome(message=speech, speech=speech, data={"line_id": line.id, "missing": missing})

    def _undo_missing(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        line = self._line(fulfillment, request.line_id)
        if line.missing == 0:
            raise ServiceError("nothing_to_undo")
        line.missing = 0
        line.missing_reason = None
        self._log(session, actor, fulfillment, request, EventKind.MISSING_CLEARED, line_id=line.id)
        speech = f"ביטלתי את הדיווח שחסר {line.title}."
        return Outcome(message=speech, speech=speech, data={"line_id": line.id})

    # Completion.

    def _complete(
        self, session: Session, actor: Principal, fulfillment: Fulfillment, request: ActionInput
    ) -> Outcome:
        progress = progress_of(fulfillment)
        if not progress.complete:
            speech = remaining_speech(fulfillment)
            raise ServiceError("incomplete_order", message=f"אי אפשר לסיים עדיין. {speech}")
        now = self.context.now()
        move(
            session, fulfillment, FulfillmentState.PACKED, actor=actor.id, now=now, reason="packed"
        )
        fulfillment.packed_at = now
        fulfillment.packed_by = actor.id
        fulfillment.assigned_to = None
        fulfillment.lease_until = None
        fulfillment.clarification = None
        fulfillment.clarification_until = None
        self._log(session, actor, fulfillment, request, EventKind.COMPLETED)
        self.completion.advance(session, fulfillment)
        speech = "ההזמנה נארזה."
        if fulfillment.state == FulfillmentState.COMPLETED:
            speech += " היא מסומנת כממתינה לאיסוף."
        return Outcome(message=speech, speech=speech)

    def reopen(self, actor: Principal, fulfillment_id: int) -> ActionResult:
        """Back from packed to packing, before anything was sent to Konimbo for pickup."""
        context = self.context
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state == FulfillmentState.COMPLETED:
                raise ServiceError("order_finished")
            if fulfillment.state != FulfillmentState.PACKED:
                raise ServiceError("invalid_state")
            if jobs_with(session, fulfillment.id, PURPOSE_PICKUP):
                raise ServiceError("reopen_blocked")
            now = context.now()
            move(
                session,
                fulfillment,
                FulfillmentState.PACKING,
                actor=actor.id,
                now=now,
                reason="reopened",
            )
            fulfillment.assigned_to = actor.id
            fulfillment.lease_until = now + timedelta(seconds=context.settings.lease_seconds)
            fulfillment.packed_at = None
            log_event(session, fulfillment, EventKind.REOPENED, actor=actor.id, now=now)
            changed(session, context.events, fulfillment, now)
            message = "ההזמנה נפתחה מחדש לאריזה."
            return ActionResult(
                ok=True,
                action=Action.REOPEN_ORDER,
                message=message,
                speech=message,
                fulfillment=self.presenter.fulfillment(session, fulfillment, actor),
            )

    # Logging.

    def _log(
        self,
        session: Session,
        actor: Principal,
        fulfillment: Fulfillment,
        request: ActionInput,
        kind: EventKind,
        **fields: Any,
    ) -> None:
        log_event(
            session,
            fulfillment,
            kind,
            actor=actor.id,
            now=self.context.now(),
            source=request.source,
            utterance_id=request.utterance_id,
            **fields,
        )
