"""Duplicate order review. The service only suggests; an employee decides every group.

Orders are never merged or cancelled because their phone numbers match. A blocking group
(likely an accidental duplicate, or orders that could leave in one box) holds its orders out
of the queue until someone decides: send together, keep separate, or mark the extra copy as
a duplicate. Marking a duplicate only takes it out of the warehouse queue and leaves a note
in Konimbo; cancelling or refunding the order stays with the office.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import (
    QUEUE_CATEGORIES,
    Category,
    EventKind,
    FulfillmentState,
    JobKind,
    ReviewDecision,
    ReviewReason,
    ReviewStatus,
)
from ..domain.errors import ServiceError
from ..domain.identity import Principal
from ..persistence.models import DuplicateReview, Fulfillment, Order
from .completion import PURPOSE_NOTE, store_job
from .context import Context
from .records import audit, changed, held_by_other, log_event, move
from .views import DuplicateOrderView, DuplicateReviewView

MERGEABLE = (
    FulfillmentState.READY,
    FulfillmentState.REVIEW,
    FulfillmentState.PAUSED,
    FulfillmentState.SKIPPED,
)


def mask_phone(value: str | None) -> str | None:
    digits = "".join(char for char in value or "" if char.isdigit())
    return f"•••{digits[-3:]}" if len(digits) >= 3 else None


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    name, domain = value.split("@", 1)
    return f"{name[:1]}•••@{domain}"


class Duplicates:
    def __init__(self, context: Context) -> None:
        self.context = context

    def reviews(self, *, pending_only: bool = True) -> list[DuplicateReviewView]:
        with self.context.db.session() as session:
            query = select(DuplicateReview).order_by(DuplicateReview.created_at.desc()).limit(100)
            if pending_only:
                query = query.where(DuplicateReview.status == ReviewStatus.PENDING)
            return [self._view(session, review) for review in session.scalars(query)]

    def _view(self, session: Session, review: DuplicateReview) -> DuplicateReviewView:
        orders = []
        for order_id in review.order_ids:
            order = session.get(Order, order_id)
            if order is None:
                continue
            fulfillment = order.fulfillment
            items = [
                f"{line.title} ×{line.required}"
                for line in (fulfillment.lines if fulfillment else [])
                if line.order_id == order.id and not line.removed
            ]
            orders.append(
                DuplicateOrderView(
                    id=order.id,
                    fulfillment_id=fulfillment.id if fulfillment else None,
                    state=FulfillmentState(fulfillment.state) if fulfillment else None,
                    customer_name=order.customer_name,
                    created_at=order.store_created_at,
                    payment_status=order.payment_status,
                    total_price=str(order.total_price) if order.total_price is not None else None,
                    items=items,
                    masked_phone=mask_phone(order.phone),
                    masked_email=mask_email(order.email),
                    suspect=order.id in review.suspects,
                )
            )
        return DuplicateReviewView(
            id=review.id,
            kind=review.kind,
            status=review.status,
            blocking=review.blocking,
            reasons=list(review.reasons),
            orders=orders,
            created_at=review.created_at,
            decided_at=review.decided_at,
            decided_by=review.decided_by,
        )

    def decide(
        self,
        actor: Principal,
        review_id: int,
        decision: ReviewDecision,
        *,
        duplicates: list[str] | None = None,
        note: str | None = None,
    ) -> DuplicateReviewView:
        context = self.context
        with context.db.session(write=True) as session:
            review = session.get(DuplicateReview, review_id, with_for_update=True)
            if review is None:
                raise ServiceError("review_not_found")
            if review.status != ReviewStatus.PENDING:
                raise ServiceError("review_decided")
            fulfillments = self._fulfillments(session, review)
            now = context.now()
            match decision:
                case ReviewDecision.CONSOLIDATE:
                    self._consolidate(session, actor, review, fulfillments)
                    review.status = ReviewStatus.CONSOLIDATED
                case ReviewDecision.KEEP_SEPARATE:
                    for fulfillment in fulfillments:
                        self._release(session, actor, fulfillment)
                    review.status = ReviewStatus.KEPT_SEPARATE
                case ReviewDecision.MARK_DUPLICATE:
                    chosen = set(duplicates or review.suspects)
                    if (
                        not chosen
                        or not chosen <= set(review.order_ids)
                        or chosen == set(review.order_ids)
                    ):
                        raise ServiceError("review_orders_changed")
                    self._mark_duplicates(session, actor, fulfillments, chosen)
                    review.status = ReviewStatus.DUPLICATE_MARKED
            review.decided_at = now
            review.decided_by = actor.id
            review.decision_note = (note or "").strip()[:500] or None
            audit(
                session,
                "duplicates.decided",
                actor=actor.id,
                now=now,
                review=review.id,
                decision=str(decision),
                orders=len(review.order_ids),
            )
            session.flush()
            return self._view(session, review)

    def _fulfillments(self, session: Session, review: DuplicateReview) -> list[Fulfillment]:
        found: dict[int, Fulfillment] = {}
        for order_id in review.order_ids:
            order = session.get(Order, order_id)
            if order is None or order.fulfillment_id is None:
                raise ServiceError("review_orders_changed")
            fulfillment = session.get(Fulfillment, order.fulfillment_id, with_for_update=True)
            if fulfillment is not None:
                found[fulfillment.id] = fulfillment
        return list(found.values())

    def _release(self, session: Session, actor: Principal, fulfillment: Fulfillment) -> None:
        if (
            fulfillment.state == FulfillmentState.REVIEW
            and fulfillment.review_reason == ReviewReason.DUPLICATE
        ):
            now = self.context.now()
            move(
                session,
                fulfillment,
                FulfillmentState.READY,
                actor=actor.id,
                now=now,
                reason="duplicate_reviewed",
            )
            fulfillment.review_reason = None
            log_event(session, fulfillment, EventKind.REVIEW_RESOLVED, actor=actor.id, now=now)
            changed(session, self.context.events, fulfillment, now)

    def _consolidate(
        self,
        session: Session,
        actor: Principal,
        review: DuplicateReview,
        fulfillments: list[Fulfillment],
    ) -> None:
        now = self.context.now()
        if len(fulfillments) < 2:
            raise ServiceError("review_orders_changed")
        for fulfillment in fulfillments:
            if fulfillment.state not in MERGEABLE or held_by_other(fulfillment, actor.id, now):
                raise ServiceError("consolidation_blocked")
            if fulfillment.state == FulfillmentState.REVIEW and fulfillment.review_reason not in (
                ReviewReason.DUPLICATE,
                None,
            ):
                # A payment question is not settled by putting orders in one box.
                raise ServiceError("consolidation_blocked")
        # The oldest order leads; the most urgent category wins.
        fulfillments.sort(key=lambda item: item.queued_at)
        primary = fulfillments[0]
        order_rank = {category: index for index, category in enumerate(QUEUE_CATEGORIES)}
        primary.category = min(
            (Category(item.category) for item in fulfillments),
            key=lambda category: order_rank.get(category, 99),
        )
        for other in fulfillments[1:]:
            for order in list(other.orders):
                order.fulfillment_id = primary.id
            for line in list(other.lines):
                line.fulfillment_id = primary.id
            for note in list(other.notes):
                note.fulfillment_id = primary.id
            session.flush()
            session.expire(other)
            move(
                session, other, FulfillmentState.CANCELLED, actor=actor.id, now=now, reason="merged"
            )
            other.merged_into_id = primary.id
            log_event(session, other, EventKind.MERGED, actor=actor.id, now=now)
            changed(session, self.context.events, other, now)
        session.flush()
        session.expire(primary)
        if primary.state != FulfillmentState.READY:
            move(
                session,
                primary,
                FulfillmentState.READY,
                actor=actor.id,
                now=now,
                reason="consolidated",
            )
        primary.review_reason = None
        log_event(session, primary, EventKind.MERGED, actor=actor.id, now=now)
        changed(session, self.context.events, primary, now)

    def _mark_duplicates(
        self,
        session: Session,
        actor: Principal,
        fulfillments: list[Fulfillment],
        chosen: set[str],
    ) -> None:
        now = self.context.now()
        config = self.context.config()
        for fulfillment in fulfillments:
            order_ids = {order.id for order in fulfillment.orders}
            if not order_ids & chosen:
                self._release(session, actor, fulfillment)
                continue
            if fulfillment.state not in MERGEABLE:
                raise ServiceError("consolidation_blocked")
            move(
                session,
                fulfillment,
                FulfillmentState.CANCELLED,
                actor=actor.id,
                now=now,
                reason="duplicate",
            )
            for order_id in sorted(order_ids & chosen):
                store_job(
                    session,
                    self.context,
                    fulfillment,
                    order_id,
                    purpose=PURPOSE_NOTE,
                    kind=JobKind.KONIMBO_NOTE,
                    title=config.note_title,
                    comment="המחסן סימן את ההזמנה ככפולה ולא יארוז אותה. ביטול וזיכוי במשרד.",
                    key=f"duplicate:{fulfillment.id}:{order_id}",
                )
            changed(session, self.context.events, fulfillment, now)
