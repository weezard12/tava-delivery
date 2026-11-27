"""The queue and moving between orders: open, next, previous, pause, skip, batches.

An employee holds one order at a time. Opening an order claims it with a lease that every
action renews; nobody else can change it while the lease runs. Leaving an order keeps its
progress: it goes back as paused, or to awaiting inventory when something was reported
missing (and only then is Konimbo told). "Previous" works like a browser's back button over
the orders this employee opened.
"""

from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..domain.enums import (
    CATEGORY_LABELS,
    QUEUE_CATEGORIES,
    Action,
    Category,
    EventKind,
    FulfillmentState,
    PrintMode,
)
from ..domain.errors import ServiceError
from ..domain.identity import Principal
from ..domain.ports import PrintCredentials
from ..persistence.models import Fulfillment, WorkSession
from .completion import PURPOSE_INVENTORY, PURPOSE_RESTORE, Completion, jobs_with, store_job
from .context import Context
from .packing import ActionInput, Packing
from .presenter import Presenter, active_lines, progress_of
from .printing import Printing
from .records import audit, changed, get_fulfillment, held_by_other, log_event, move
from .views import ActionResult, FulfillmentView, Position, QueueCount, QueueView, WorkView

HISTORY_LIMIT = 50
QUEUE_LIMIT = 300
# How long an order may sit claimed with nobody acting on it before it is paused for others.
ABANDONED_AFTER = timedelta(minutes=30)
CLAIMABLE = (
    FulfillmentState.READY,
    FulfillmentState.PAUSED,
    FulfillmentState.SKIPPED,
    FulfillmentState.AWAITING_INVENTORY,
    FulfillmentState.PACKING,
)
VIEW_ONLY = (FulfillmentState.PACKED, FulfillmentState.COMPLETED, FulfillmentState.REVIEW)
LISTED = (
    FulfillmentState.READY,
    FulfillmentState.PACKING,
    FulfillmentState.PAUSED,
    FulfillmentState.SKIPPED,
    FulfillmentState.REVIEW,
    FulfillmentState.AWAITING_INVENTORY,
    FulfillmentState.PACKED,
)
CATEGORY_ORDER = {category: index for index, category in enumerate(QUEUE_CATEGORIES)}


class Work:
    def __init__(
        self,
        context: Context,
        completion: Completion,
        presenter: Presenter,
        packing: Packing,
        printing: Printing,
    ) -> None:
        self.context = context
        self.completion = completion
        self.presenter = presenter
        self.packing = packing
        self.printing = printing

    # The employee's place.

    def _session_of(self, session: Session, actor: Principal) -> WorkSession:
        row = session.get(WorkSession, actor.id, with_for_update=True)
        if row is None:
            row = WorkSession(actor=actor.id, history=[], batch=[], updated_at=self.context.now())
            session.add(row)
            session.flush()
        return row

    def current(self, actor: Principal) -> WorkView:
        with self.context.db.session() as session:
            row = session.get(WorkSession, actor.id)
            fulfillment = None
            if row is not None and row.current_id is not None:
                fulfillment = session.get(Fulfillment, row.current_id)
            return self._view(session, actor, row, fulfillment)

    def _view(
        self,
        session: Session,
        actor: Principal,
        row: WorkSession | None,
        fulfillment: Fulfillment | None,
    ) -> WorkView:
        category = Category(row.category) if row is not None and row.category else None
        batch = list(row.batch) if row is not None else []
        history = list(row.history) if row is not None else []
        if batch:
            index = (
                batch.index(fulfillment.id) + 1 if fulfillment and fulfillment.id in batch else 0
            )
            total = len(batch)
        else:
            index = len(history) + (1 if fulfillment else 0)
            total = index
        position = Position(
            index=index,
            total=total,
            remaining_in_queue=self._ready_count(session, category, batch),
            can_go_back=bool(history),
            batch=bool(batch),
            category=category,
        )
        view = self.presenter.fulfillment(session, fulfillment, actor) if fulfillment else None
        return WorkView(fulfillment=view, position=position)

    def _ready_count(self, session: Session, category: Category | None, batch: list[int]) -> int:
        query = select(func.count(Fulfillment.id)).where(
            Fulfillment.state.in_([FulfillmentState.READY, FulfillmentState.PAUSED])
        )
        if batch:
            query = query.where(Fulfillment.id.in_(batch))
        elif category is not None:
            query = query.where(Fulfillment.category == category)
        return int(session.scalar(query) or 0)

    # Opening and leaving.

    def show(self, actor: Principal, fulfillment_id: int) -> FulfillmentView:
        """Look at an order without taking it, for example a finished one."""
        with self.context.db.session() as session:
            fulfillment = get_fulfillment(session, fulfillment_id)
            return self.presenter.fulfillment(session, fulfillment, actor)

    def open(self, actor: Principal, fulfillment_id: int) -> WorkView:
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            self._move_to(session, actor, row, fulfillment)
            return self._view(session, actor, row, fulfillment)

    def _move_to(
        self, session: Session, actor: Principal, row: WorkSession, fulfillment: Fulfillment
    ) -> None:
        """Leave the current order (keeping its progress) and take this one."""
        if row.current_id is not None and row.current_id != fulfillment.id:
            previous = session.get(Fulfillment, row.current_id, with_for_update=True)
            if previous is not None:
                self._leave(session, actor, previous)
            history = [entry for entry in row.history if entry != row.current_id]
            row.history = [*history, row.current_id][-HISTORY_LIMIT:]
        self._claim(session, actor, fulfillment)
        row.current_id = fulfillment.id
        row.updated_at = self.context.now()

    def _claim(self, session: Session, actor: Principal, fulfillment: Fulfillment) -> None:
        now = self.context.now()
        state = FulfillmentState(fulfillment.state)
        if state in VIEW_ONLY:
            # Packed and completed orders open for viewing and printing only.
            return
        if state is FulfillmentState.CANCELLED:
            raise ServiceError("invalid_state")
        if held_by_other(fulfillment, actor.id, now):
            raise ServiceError("fulfillment_locked")
        if state is not FulfillmentState.PACKING:
            move(
                session,
                fulfillment,
                FulfillmentState.PACKING,
                actor=actor.id,
                now=now,
                reason="claimed",
            )
        fulfillment.assigned_to = actor.id
        fulfillment.lease_until = now + timedelta(seconds=self.context.settings.lease_seconds)
        fulfillment.skip_reason = None
        log_event(session, fulfillment, EventKind.CLAIMED, actor=actor.id, now=now)
        if not fulfillment.preparing_sent:
            self.completion.on_claim(session, fulfillment)
        changed(session, self.context.events, fulfillment, now)

    def _leave(self, session: Session, actor: Principal, fulfillment: Fulfillment) -> None:
        if fulfillment.state != FulfillmentState.PACKING or fulfillment.assigned_to != actor.id:
            return
        now = self.context.now()
        lines = active_lines(fulfillment)
        if any(line.missing for line in lines):
            self._to_awaiting_inventory(session, actor, fulfillment)
        else:
            progress = progress_of(fulfillment)
            touched = progress.units_confirmed > 0 or any(
                note.confirmed_at for note in fulfillment.notes
            )
            target = FulfillmentState.PAUSED if touched else FulfillmentState.READY
            move(session, fulfillment, target, actor=actor.id, now=now, reason="left")
        fulfillment.assigned_to = None
        fulfillment.lease_until = None
        fulfillment.clarification = None
        fulfillment.clarification_until = None
        log_event(session, fulfillment, EventKind.RELEASED, actor=actor.id, now=now)
        changed(session, self.context.events, fulfillment, now)

    def _to_awaiting_inventory(
        self, session: Session, actor: Principal, fulfillment: Fulfillment
    ) -> None:
        now = self.context.now()
        move(
            session,
            fulfillment,
            FulfillmentState.AWAITING_INVENTORY,
            actor=actor.id,
            now=now,
            reason="missing_inventory",
        )
        config = self.context.config()
        count = len(jobs_with(session, fulfillment.id, PURPOSE_INVENTORY))
        for order in fulfillment.orders:
            # The comment is written when the job runs, from what is missing at that moment.
            store_job(
                session,
                self.context,
                fulfillment,
                order.id,
                purpose=PURPOSE_INVENTORY,
                title=config.status_awaiting_inventory,
                comment="",
                key=f"inventory:{fulfillment.id}:{order.id}:{count + 1}",
            )
        audit(session, "inventory.missing", actor=actor.id, now=now, fulfillment_id=fulfillment.id)

    # Navigation.

    def next(self, actor: Principal) -> WorkView:
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            candidate = self._next_candidate(session, actor, row)
            if candidate is None:
                if row.current_id is not None:
                    current = session.get(Fulfillment, row.current_id, with_for_update=True)
                    if current is not None:
                        self._leave(session, actor, current)
                        row.history = [
                            *[entry for entry in row.history if entry != row.current_id],
                            row.current_id,
                        ][-HISTORY_LIMIT:]
                    row.current_id = None
                raise ServiceError("no_next_order")
            self._move_to(session, actor, row, candidate)
            return self._view(session, actor, row, candidate)

    def _next_candidate(
        self, session: Session, actor: Principal, row: WorkSession
    ) -> Fulfillment | None:
        now = self.context.now()
        if row.batch:
            for fulfillment_id in row.batch:
                if fulfillment_id == row.current_id:
                    continue
                fulfillment = session.get(Fulfillment, fulfillment_id, with_for_update=True)
                if (
                    fulfillment is not None
                    and fulfillment.state in (FulfillmentState.READY, FulfillmentState.PAUSED)
                    and not held_by_other(fulfillment, actor.id, now)
                ):
                    return fulfillment
            return None
        # Rank without locking, then lock one order at a time: locking every candidate would
        # leave nothing for a packer asking at the same moment.
        rank = case(
            {category.value: index for category, index in CATEGORY_ORDER.items()},
            value=Fulfillment.category,
            else_=len(CATEGORY_ORDER),
        )
        query = select(Fulfillment.id).where(Fulfillment.state == FulfillmentState.READY)
        if row.category:
            query = query.where(Fulfillment.category == row.category)
        if row.current_id is not None:
            query = query.where(Fulfillment.id != row.current_id)
        ids = session.scalars(
            query.order_by(rank, Fulfillment.queued_at, Fulfillment.id).limit(QUEUE_LIMIT)
        ).all()
        for fulfillment_id in ids:
            fulfillment = session.scalars(
                select(Fulfillment)
                .where(
                    Fulfillment.id == fulfillment_id,
                    Fulfillment.state == FulfillmentState.READY,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).first()
            if fulfillment is not None and not held_by_other(fulfillment, actor.id, now):
                return fulfillment
        return None

    def previous(self, actor: Principal) -> WorkView:
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            history = list(row.history)
            while history:
                fulfillment_id = history.pop()
                fulfillment = session.get(Fulfillment, fulfillment_id, with_for_update=True)
                if fulfillment is None or fulfillment.state == FulfillmentState.CANCELLED:
                    continue
                if fulfillment.state == FulfillmentState.PACKING and held_by_other(
                    fulfillment, actor.id, context.now()
                ):
                    continue
                current = row.current_id
                if current is not None:
                    leaving = session.get(Fulfillment, current, with_for_update=True)
                    if leaving is not None:
                        self._leave(session, actor, leaving)
                self._claim(session, actor, fulfillment)
                row.history = history
                row.current_id = fulfillment.id
                row.updated_at = context.now()
                return self._view(session, actor, row, fulfillment)
            row.history = []
            raise ServiceError("no_previous_order")

    def release(self, actor: Principal, fulfillment_id: int) -> WorkView:
        """Pause: put the order back with its progress, and stand on nothing."""
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state != FulfillmentState.PACKING:
                raise ServiceError("not_claimed")
            if fulfillment.assigned_to != actor.id:
                raise ServiceError("not_claimed")
            self._leave(session, actor, fulfillment)
            if row.current_id == fulfillment_id:
                row.history = [
                    *[entry for entry in row.history if entry != fulfillment_id],
                    fulfillment_id,
                ][-HISTORY_LIMIT:]
                row.current_id = None
            return self._view(session, actor, row, None)

    def skip(self, actor: Principal, fulfillment_id: int, reason: str | None) -> WorkView:
        text = (reason or "").strip()
        if not text:
            raise ServiceError("skip_reason_required")
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state != FulfillmentState.PACKING or fulfillment.assigned_to != actor.id:
                raise ServiceError("not_claimed")
            now = context.now()
            move(
                session,
                fulfillment,
                FulfillmentState.SKIPPED,
                actor=actor.id,
                now=now,
                reason="skipped",
            )
            fulfillment.skip_reason = text[:500]
            fulfillment.assigned_to = None
            fulfillment.lease_until = None
            log_event(
                session, fulfillment, EventKind.SKIPPED, actor=actor.id, now=now, reason=text[:500]
            )
            changed(session, context.events, fulfillment, now)
            if row.current_id == fulfillment_id:
                row.history = [*row.history, fulfillment_id][-HISTORY_LIMIT:]
                row.current_id = None
            candidate = self._next_candidate(session, actor, row)
            if candidate is not None:
                self._claim(session, actor, candidate)
                row.current_id = candidate.id
            return self._view(session, actor, row, candidate)

    def heartbeat(self, actor: Principal, fulfillment_id: int) -> None:
        context = self.context
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state != FulfillmentState.PACKING:
                return
            if fulfillment.assigned_to != actor.id:
                raise ServiceError(
                    "fulfillment_locked"
                    if held_by_other(fulfillment, actor.id, context.now())
                    else "not_claimed"
                )
            fulfillment.lease_until = context.now() + timedelta(
                seconds=context.settings.lease_seconds
            )

    # Choosing what to work on.

    def choose(
        self, actor: Principal, *, category: Category | None, batch: list[int] | None
    ) -> WorkView:
        """Work through one category, or a batch of chosen orders, or everything (neither)."""
        context = self.context
        with context.db.session(write=True) as session:
            row = self._session_of(session, actor)
            if batch is not None:
                ids = list(dict.fromkeys(batch))[:QUEUE_LIMIT]
                if not ids:
                    raise ServiceError("batch_empty")
                row.batch = ids
                row.category = None
            else:
                row.batch = []
                row.category = category
            row.updated_at = context.now()
            current = session.get(Fulfillment, row.current_id) if row.current_id else None
            return self._view(session, actor, row, current)

    def queue(self, actor: Principal, category: Category | None = None) -> QueueView:
        with self.context.db.session() as session:
            counts = []
            for item in QUEUE_CATEGORIES:
                ready = session.scalar(
                    select(func.count(Fulfillment.id)).where(
                        Fulfillment.category == item,
                        Fulfillment.state == FulfillmentState.READY,
                    )
                )
                busy = session.scalar(
                    select(func.count(Fulfillment.id)).where(
                        Fulfillment.category == item,
                        Fulfillment.state.in_(
                            [
                                FulfillmentState.PACKING,
                                FulfillmentState.PAUSED,
                                FulfillmentState.SKIPPED,
                            ]
                        ),
                    )
                )
                counts.append(
                    QueueCount(
                        category=item,
                        label=CATEGORY_LABELS[item],
                        ready=int(ready or 0),
                        in_progress=int(busy or 0),
                    )
                )
            awaiting = session.scalar(
                select(func.count(Fulfillment.id)).where(
                    Fulfillment.state == FulfillmentState.AWAITING_INVENTORY
                )
            )
            review = session.scalar(
                select(func.count(Fulfillment.id)).where(
                    Fulfillment.state == FulfillmentState.REVIEW
                )
            )
            query = select(Fulfillment).where(Fulfillment.state.in_(list(LISTED)))
            if category is not None:
                query = query.where(Fulfillment.category == category)
            rows = session.scalars(
                query.order_by(Fulfillment.queued_at, Fulfillment.id).limit(QUEUE_LIMIT)
            ).all()
            ordered = sorted(
                rows,
                key=lambda item: (
                    CATEGORY_ORDER.get(Category(item.category), 99),
                    item.queued_at,
                ),
            )
            return QueueView(
                counts=counts,
                awaiting_inventory=int(awaiting or 0),
                review=int(review or 0),
                items=[self.presenter.queue_item(item, actor) for item in ordered],
            )

    # Inventory came back.

    def resolve_inventory(self, actor: Principal, fulfillment_id: int) -> None:
        """Stock arrived: back to the queue with its progress, and Konimbo moves back too."""
        context = self.context
        with context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            if fulfillment.state != FulfillmentState.AWAITING_INVENTORY:
                raise ServiceError("not_awaiting_inventory")
            now = context.now()
            for line in fulfillment.lines:
                line.missing = 0
                line.missing_reason = None
            move(
                session,
                fulfillment,
                FulfillmentState.READY,
                actor=actor.id,
                now=now,
                reason="inventory_resolved",
            )
            fulfillment.queued_at = min(fulfillment.queued_at, now)
            log_event(session, fulfillment, EventKind.INVENTORY_RESOLVED, actor=actor.id, now=now)
            count = len(jobs_with(session, fulfillment.id, PURPOSE_RESTORE))
            for order in fulfillment.orders:
                if order.origin_status and order.status_title != order.origin_status:
                    store_job(
                        session,
                        context,
                        fulfillment,
                        order.id,
                        purpose=PURPOSE_RESTORE,
                        title=order.origin_status,
                        comment="המלאי הגיע, ההזמנה חזרה לתור האריזה.",
                        key=f"restore:{fulfillment.id}:{order.id}:{count + 1}",
                    )
            audit(
                session,
                "inventory.resolved",
                actor=actor.id,
                now=now,
                fulfillment_id=fulfillment.id,
            )
            changed(session, context.events, fulfillment, now)

    # Housekeeping (scheduler).

    def pause_abandoned(self) -> int:
        """Orders claimed and left untouched for a long time go back for anyone to resume."""
        context = self.context
        cutoff = context.now() - ABANDONED_AFTER
        paused = 0
        with context.db.session(write=True) as session:
            rows = session.scalars(
                select(Fulfillment)
                .where(
                    Fulfillment.state == FulfillmentState.PACKING,
                    Fulfillment.lease_until.is_not(None),
                    Fulfillment.lease_until < cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(50)
            ).all()
            for fulfillment in rows:
                holder = Principal(fulfillment.assigned_to or "system")
                self._leave(session, holder, fulfillment)
                paused += 1
        return paused

    # The one entry point for every action, from a button or from the voice assistant.

    def act(
        self,
        actor: Principal,
        fulfillment_id: int,
        request: ActionInput,
        *,
        credentials: PrintCredentials,
        auto_next: bool = False,
    ) -> ActionResult:
        match request.action:
            case Action.NEXT_ORDER:
                view = self.next(actor)
                return self._navigated(Action.NEXT_ORDER, view)
            case Action.PREVIOUS_ORDER:
                view = self.previous(actor)
                return self._navigated(Action.PREVIOUS_ORDER, view)
            case Action.PAUSE_ORDER:
                self.release(actor, fulfillment_id)
                message = "ההזמנה הושהתה. ההתקדמות נשמרה."
                return ActionResult(ok=True, action=request.action, message=message, speech=message)
            case Action.SKIP_ORDER:
                view = self.skip(actor, fulfillment_id, request.reason)
                message = "דילגתי על ההזמנה."
                result = self._navigated(Action.SKIP_ORDER, view)
                result.message = message
                result.speech = message + (" " + result.speech if view.fulfillment else "")
                return result
            case Action.REOPEN_ORDER:
                return self.packing.reopen(actor, fulfillment_id)
            case Action.PRINT_SHIPPING_LABEL:
                return self.printing.print_label(
                    actor,
                    fulfillment_id,
                    mode=request.print_mode or PrintMode.MANUAL,
                    credentials=credentials,
                    idempotency_key=request.idempotency_key,
                )
        result = self.packing.execute(actor, fulfillment_id, request)
        if request.action is Action.COMPLETE_ORDER and auto_next:
            try:
                view = self.next(actor)
            except ServiceError:
                result.speech += " אין עוד הזמנות בתור."
            else:
                result.navigated_to = view.fulfillment.id if view.fulfillment else None
                result.speech += " " + self._announce(view)
        return result

    def _navigated(self, action: Action, view: WorkView) -> ActionResult:
        speech = self._announce(view)
        return ActionResult(
            ok=True,
            action=action,
            message=speech,
            speech=speech,
            fulfillment=view.fulfillment,
            navigated_to=view.fulfillment.id if view.fulfillment else None,
        )

    def _announce(self, view: WorkView) -> str:
        fulfillment = view.fulfillment
        if fulfillment is None:
            return "אין הזמנה פתוחה."
        order = fulfillment.orders[0] if fulfillment.orders else None
        name = f" של {order.customer_name}" if order and order.customer_name else ""
        number = f" {order.id}" if order else ""
        lines = sum(1 for line in fulfillment.lines if not line.removed)
        return f"הזמנה{number}{name}, {lines} מוצרים."
