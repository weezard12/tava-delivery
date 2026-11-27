"""The Konimbo import: the orders in the warehouse's statuses, kept up to date.

Only orders created in the last ``import_days`` and in one of the configured statuses are
read, with only the fields the warehouse needs (never payment card data). Konimbo stays the
source of every commercial fact; packing progress, confirmations and the event log stay here
and survive every re-import. An order that leaves the warehouse's statuses before packing
starts leaves the queue; one already in progress is flagged for a person instead.
"""

import hashlib
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.duplicates import OrderFacts, address_key, email_key, find_groups, phone_key
from ..domain.enums import (
    ACTIVE_JOB_STATES,
    BLOCKING_DUPLICATE_KINDS,
    Category,
    EventKind,
    FulfillmentState,
    LineKind,
    NoteKind,
    NoteSource,
    ReviewReason,
    ReviewStatus,
)
from ..domain.errors import ProviderError
from ..domain.identity import SYSTEM_ACTOR
from ..domain.notes import split_instructions
from ..domain.ports import StoreOrder
from ..persistence.models import (
    DuplicateReview,
    Fulfillment,
    Line,
    Note,
    Order,
    Product,
    StatusOption,
    SyncRun,
)
from .completion import OWN_MARKER, PURPOSE_INVENTORY, jobs_with
from .context import Context
from .records import audit, changed, log_event, move
from .warehouse_config import WarehouseConfig

logger = logging.getLogger("teva_delivery.sync")

# States in which a re-import may still change what the employee sees.
OPEN = (
    FulfillmentState.READY,
    FulfillmentState.REVIEW,
    FulfillmentState.PAUSED,
    FulfillmentState.SKIPPED,
    FulfillmentState.AWAITING_INVENTORY,
    FulfillmentState.PACKING,
)
UNTOUCHED = (FulfillmentState.READY, FulfillmentState.REVIEW)
DUPLICATE_LOOKBACK = timedelta(days=14)
STATUS_DISCOVERY_ORDERS = 100


def note_key(source: NoteSource, text: str, index: int) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"{source}:{index}:{digest}"


def items_signature(order: StoreOrder) -> str:
    items = sorted(
        (line.sku or line.item_id or line.title, line.quantity)
        for line in order.lines
        if line.kind is LineKind.PRODUCT
    )
    return ";".join(f"{key}x{quantity}" for key, quantity in items)


class Sync:
    def __init__(self, context: Context) -> None:
        self.context = context

    # The import.

    def run(self) -> SyncRun:
        context = self.context
        store = context.providers.store
        started = context.now()
        counts = {"seen": 0, "created": 0, "updated": 0, "left": 0, "skipped": 0, "reviews": 0}
        error: str | None = None
        if store is None:
            return self._record(started, "skipped", "konimbo_not_configured", counts)
        config = context.config()
        created_after = started - timedelta(days=context.settings.import_days)
        seen: dict[str, StoreOrder] = {}
        complete = True
        for status in config.tracked_statuses:
            try:
                for order in store.list_orders(status, created_after):
                    seen[order.id] = order
            except ProviderError as failure:
                complete = False
                error = failure.code
                logger.warning("Konimbo import stopped", extra={"event": {"code": failure.code}})
                break
        counts["seen"] = len(seen)
        with context.db.session(write=True) as session:
            for order in seen.values():
                outcome = self._upsert(session, order, config)
                counts[outcome] += 1
            self._remember_statuses(session, seen.values())
            if complete:
                counts["left"] = self._scope_out(session, set(seen), created_after)
            counts["reviews"] = self._duplicates(session, config)
        self._fetch_images()
        outcome = "succeeded" if complete else ("partial" if seen else "failed")
        return self._record(started, outcome, error, counts)

    def _record(
        self, started: datetime, outcome: str, error: str | None, counts: dict[str, int]
    ) -> SyncRun:
        with self.context.db.session(write=True) as session:
            run = SyncRun(
                kind="konimbo.import",
                started_at=started,
                finished_at=self.context.now(),
                outcome=outcome,
                error=error,
                counts=counts,
            )
            session.add(run)
        self.context.events.queue_changed()
        return run

    # One order.

    def _upsert(self, session: Session, store_order: StoreOrder, config: WarehouseConfig) -> str:
        now = self.context.now()
        status = store_order.status_title or ""
        category = config.category_by_status.get(status)
        row = session.get(Order, store_order.id)
        failed_payment = (store_order.payment_status or "") in config.failed_payment_statuses
        if row is None:
            if category is None or failed_payment:
                # Not one of the warehouse's categories (one of its own statuses set by
                # someone else), or a failed payment: never enters the queue.
                return "skipped"
            row = Order(id=store_order.id, imported_at=now, origin_status=status)
            self._copy(row, store_order, now)
            session.add(row)
            fulfillment = self._new_fulfillment(session, row, store_order, category, config)
            row.fulfillment_id = fulfillment.id
            session.flush()
            self._sync_lines(session, fulfillment, row, store_order)
            self._sync_notes(session, fulfillment, row, store_order)
            changed(session, self.context.events, fulfillment, now)
            return "created"
        self._copy(row, store_order, now)
        current = (
            session.get(Fulfillment, row.fulfillment_id, with_for_update=True)
            if row.fulfillment_id
            else None
        )
        if current is None or current.state not in OPEN:
            return "updated"
        fulfillment = current
        if category is not None:
            row.origin_status = status
        self._follow_status(session, fulfillment, row, category, failed_payment, config)
        lines_changed = self._sync_lines(session, fulfillment, row, store_order)
        self._sync_notes(session, fulfillment, row, store_order)
        if lines_changed and fulfillment.state == FulfillmentState.PACKING:
            fulfillment.upstream_warning = "store_order_changed"
            log_event(session, fulfillment, EventKind.UPSTREAM_CHANGED, actor=SYSTEM_ACTOR, now=now)
        changed(session, self.context.events, fulfillment, now)
        return "updated"

    def _copy(self, row: Order, store_order: StoreOrder, now: datetime) -> None:
        row.store_created_at = store_order.created_at
        row.synced_at = now
        row.status_title = store_order.status_title
        row.payment_status = store_order.payment_status
        row.customer_name = store_order.customer_name
        row.phone = store_order.phone
        row.email = store_order.email
        row.address = store_order.address
        row.phone_key = phone_key(store_order.phone)
        row.email_key = email_key(store_order.email)
        row.address_key = address_key(store_order.address)
        row.note = store_order.note
        row.total_price = store_order.total_price
        row.shipping_id = store_order.shipping_id
        row.shipping_title = store_order.shipping_title
        row.items_signature = items_signature(store_order)
        row.in_scope = True

    def _new_fulfillment(
        self,
        session: Session,
        row: Order,
        store_order: StoreOrder,
        category: Category,
        config: WarehouseConfig,
    ) -> Fulfillment:
        now = self.context.now()
        state = FulfillmentState.READY
        reason = None
        if category is Category.AWAITING_INVENTORY:
            state = FulfillmentState.AWAITING_INVENTORY
        elif config.paid_statuses and (store_order.payment_status or "") not in (
            config.paid_statuses
        ):
            state = FulfillmentState.REVIEW
            reason = ReviewReason.PAYMENT_UNVERIFIED
        fulfillment = Fulfillment(
            state=state,
            category=category,
            primary_order_id=row.id,
            queued_at=store_order.created_at,
            created_at=now,
            updated_at=now,
            shipping_profile=config.shipping_profile(category, store_order.shipping_title),
            review_reason=reason,
        )
        session.add(fulfillment)
        session.flush()
        audit(
            session,
            "order.imported",
            actor=SYSTEM_ACTOR,
            now=now,
            fulfillment_id=fulfillment.id,
            order=row.id,
            state=str(state),
        )
        return fulfillment

    def _follow_status(
        self,
        session: Session,
        fulfillment: Fulfillment,
        row: Order,
        category: Category | None,
        failed_payment: bool,
        config: WarehouseConfig,
    ) -> None:
        """Konimbo moved the order: follow the office's decision where it is safe to."""
        now = self.context.now()
        state = FulfillmentState(fulfillment.state)
        if failed_payment and state in UNTOUCHED:
            move(
                session,
                fulfillment,
                FulfillmentState.REVIEW,
                actor=SYSTEM_ACTOR,
                now=now,
                reason="payment_failed",
            )
            fulfillment.review_reason = ReviewReason.PAYMENT_FAILED
            return
        if (
            state is FulfillmentState.REVIEW
            and fulfillment.review_reason == ReviewReason.PAYMENT_UNVERIFIED
            and config.paid_statuses
            and all(
                (order.payment_status or "") in config.paid_statuses for order in fulfillment.orders
            )
        ):
            move(
                session,
                fulfillment,
                FulfillmentState.READY,
                actor=SYSTEM_ACTOR,
                now=now,
                reason="payment_confirmed",
            )
            fulfillment.review_reason = None
        if category is None:
            # One of the service's own statuses (packing started, sent to Cargo): no change.
            return
        if category is Category.AWAITING_INVENTORY:
            if state in (FulfillmentState.READY, FulfillmentState.PAUSED, FulfillmentState.SKIPPED):
                move(
                    session,
                    fulfillment,
                    FulfillmentState.AWAITING_INVENTORY,
                    actor=SYSTEM_ACTOR,
                    now=now,
                    reason="store_awaiting_inventory",
                )
            elif state is FulfillmentState.PACKING:
                fulfillment.upstream_warning = "store_awaiting_inventory"
            return
        if state is FulfillmentState.AWAITING_INVENTORY:
            inventory = jobs_with(session, fulfillment.id, PURPOSE_INVENTORY)
            last = inventory[-1] if inventory else None
            if last is not None and (last.state in ACTIVE_JOB_STATES or last.dry_run):
                # Our own update is on its way, or was only recorded (test mode).
                return
            # The office moved it back to a packing category: the stock is here.
            for line in fulfillment.lines:
                line.missing = 0
                line.missing_reason = None
            move(
                session,
                fulfillment,
                FulfillmentState.READY,
                actor=SYSTEM_ACTOR,
                now=now,
                reason="store_inventory_resolved",
            )
            log_event(
                session, fulfillment, EventKind.INVENTORY_RESOLVED, actor=SYSTEM_ACTOR, now=now
            )
        if row.id == fulfillment.primary_order_id and fulfillment.category != category:
            # The office moved the order to another category (for example to urgent).
            fulfillment.category = category

    def _sync_lines(
        self, session: Session, fulfillment: Fulfillment, row: Order, store_order: StoreOrder
    ) -> bool:
        existing = {line.store_key: line for line in fulfillment.lines if line.order_id == row.id}
        position = max((line.position for line in fulfillment.lines), default=-1) + 1
        changed_lines = False
        keys = set()
        for store_line in store_order.lines:
            keys.add(store_line.key)
            line = existing.get(store_line.key)
            if line is None:
                fulfillment.lines.append(
                    Line(
                        order_id=row.id,
                        store_key=store_line.key,
                        position=position,
                        kind=store_line.kind,
                        item_id=store_line.item_id,
                        sku=store_line.sku,
                        title=store_line.title,
                        required=store_line.quantity,
                        confirmed=0,
                        missing=0,
                    )
                )
                position += 1
                changed_lines = changed_lines or bool(existing)
                continue
            if (
                line.removed
                or line.required != store_line.quantity
                or line.title != store_line.title
            ):
                changed_lines = True
            line.removed = False
            line.title = store_line.title
            line.required = store_line.quantity
            line.confirmed = min(line.confirmed, line.required)
            line.missing = min(line.missing, line.required)
        for key, line in existing.items():
            if key not in keys and not line.removed:
                line.removed = True
                changed_lines = True
        return changed_lines

    def _sync_notes(
        self, session: Session, fulfillment: Fulfillment, row: Order, store_order: StoreOrder
    ) -> None:
        wanted: list[tuple[str, NoteSource, NoteKind, str]] = []
        for index, item in enumerate(
            split_instructions(store_order.note, default=NoteKind.PACKING)
        ):
            wanted.append(
                (
                    note_key(NoteSource.CUSTOMER, item.text, index),
                    NoteSource.CUSTOMER,
                    item.kind,
                    item.text,
                )
            )
        username = self.context.settings.konimbo_status_username
        for status in store_order.statuses:
            comment = (status.comment or "").strip()
            if not comment or OWN_MARKER in comment or status.username == username:
                continue
            for index, item in enumerate(split_instructions(comment, default=NoteKind.INFO)):
                wanted.append(
                    (
                        note_key(NoteSource.OFFICE, item.text, index),
                        NoteSource.OFFICE,
                        item.kind,
                        item.text,
                    )
                )
        existing = {note.store_key: note for note in fulfillment.notes if note.order_id == row.id}
        position = max((note.position for note in fulfillment.notes), default=-1) + 1
        keys = {key for key, *_ in wanted}
        for key, source, kind, text in wanted:
            if key in existing:
                continue
            fulfillment.notes.append(
                Note(
                    order_id=row.id,
                    store_key=key,
                    position=position,
                    source=source,
                    kind=kind,
                    text=text,
                )
            )
            position += 1
        for key, note in existing.items():
            # A note the customer or office rewrote: the old text goes unless it was done.
            if (
                key not in keys
                and note.source != NoteSource.WAREHOUSE
                and note.confirmed_at is None
            ):
                fulfillment.notes.remove(note)

    # Leaving the warehouse's statuses.

    def _scope_out(self, session: Session, seen: set[str], created_after: datetime) -> int:
        now = self.context.now()
        rows = session.scalars(
            select(Fulfillment).where(Fulfillment.state.in_(list(OPEN))).with_for_update()
        ).all()
        left = 0
        for fulfillment in rows:
            orders = fulfillment.orders
            if not orders or any(order.id in seen for order in orders):
                continue
            if any(order.store_created_at < created_after for order in orders):
                # Older than the import window: not seen because it was not asked for.
                continue
            for order in orders:
                order.in_scope = False
            if fulfillment.state in UNTOUCHED:
                move(
                    session,
                    fulfillment,
                    FulfillmentState.CANCELLED,
                    actor=SYSTEM_ACTOR,
                    now=now,
                    reason="left_scope",
                )
            else:
                fulfillment.upstream_warning = "order_left_scope"
            changed(session, self.context.events, fulfillment, now)
            left += 1
        return left

    # Duplicate orders.

    def _duplicates(self, session: Session, config: WarehouseConfig) -> int:
        now = self.context.now()
        since = now - DUPLICATE_LOOKBACK
        orders = session.scalars(select(Order).where(Order.store_created_at >= since)).all()
        facts = []
        for order in orders:
            fulfillment = order.fulfillment
            finished = fulfillment is None or fulfillment.state not in OPEN
            facts.append(
                OrderFacts(
                    order_id=order.id,
                    created_at=order.store_created_at,
                    phone=order.phone,
                    email=order.email,
                    address=order.address,
                    items=tuple(
                        (part.rsplit("x", 1)[0], int(part.rsplit("x", 1)[1]))
                        for part in order.items_signature.split(";")
                        if "x" in part and part.rsplit("x", 1)[1].isdigit()
                    ),
                    total=str(order.total_price.quantize(Decimal("0.01")))
                    if order.total_price is not None
                    else None,
                    payment_failed=(order.payment_status or "") in config.failed_payment_statuses,
                    finished=finished,
                )
            )
        created = 0
        by_id = {order.id: order for order in orders}
        for group in find_groups(facts):
            review = session.scalars(
                select(DuplicateReview).where(DuplicateReview.signature == group.signature)
            ).one_or_none()
            if review is not None:
                continue
            blocking = group.kind in BLOCKING_DUPLICATE_KINDS
            session.add(
                DuplicateReview(
                    signature=group.signature,
                    kind=group.kind,
                    status=ReviewStatus.PENDING,
                    blocking=blocking,
                    order_ids=list(group.order_ids),
                    suspects=list(group.suspects),
                    reasons=list(group.reasons),
                    created_at=now,
                )
            )
            created += 1
            if not blocking:
                continue
            for order_id in group.order_ids:
                fulfillment = by_id[order_id].fulfillment
                if fulfillment is not None and fulfillment.state == FulfillmentState.READY:
                    move(
                        session,
                        fulfillment,
                        FulfillmentState.REVIEW,
                        actor=SYSTEM_ACTOR,
                        now=now,
                        reason="duplicate",
                    )
                    fulfillment.review_reason = ReviewReason.DUPLICATE
                    changed(session, self.context.events, fulfillment, now)
        return created

    # Status names for the settings screen.

    def _remember_statuses(self, session: Session, orders: Iterable[StoreOrder]) -> None:
        now = self.context.now()
        counts: dict[str, int] = {}
        for order in orders:
            if order.status_title:
                counts[order.status_title] = counts.get(order.status_title, 0) + 1
            for status in order.statuses:
                if status.title:
                    counts.setdefault(status.title, 0)
        for title, seen in counts.items():
            title = title.strip()[:200]
            if not title:
                continue
            option = session.get(StatusOption, title)
            if option is None:
                session.add(StatusOption(title=title, last_seen_at=now, orders_seen=seen))
            else:
                option.last_seen_at = now
                option.orders_seen = seen or option.orders_seen

    def discover_statuses(self) -> int:
        """Read recent orders in any status, only to learn the store's status names."""
        store = self.context.providers.store
        if store is None:
            return 0
        since = self.context.now() - timedelta(days=30)
        try:
            orders = list(store.list_orders(None, since, limit=STATUS_DISCOVERY_ORDERS))
        except ProviderError:
            return 0
        with self.context.db.session(write=True) as session:
            self._remember_statuses(session, orders)
        return len(orders)

    # Product pictures.

    def _fetch_images(self) -> None:
        context = self.context
        store = context.providers.store
        limit = context.settings.product_image_fetches_per_sync
        if store is None or not context.settings.product_images or limit == 0:
            return
        with context.db.session() as session:
            known = select(Product.item_id)
            wanted = session.scalars(
                select(Line.item_id)
                .join(Fulfillment, Fulfillment.id == Line.fulfillment_id)
                .where(
                    Line.item_id.is_not(None),
                    Line.item_id.not_in(known),
                    Fulfillment.state.in_(list(OPEN)),
                )
                .distinct()
                .limit(limit)
            ).all()
        for item_id in wanted:
            if item_id is None or store.requests_left < 10:
                break
            try:
                product = store.get_product(item_id)
            except ProviderError:
                break
            with context.db.session(write=True) as session:
                if session.get(Product, item_id) is not None:
                    continue
                session.add(
                    Product(
                        item_id=item_id,
                        title=product.title if product else None,
                        image_url=product.image_url if product else None,
                        fetched_at=context.now(),
                        found=product is not None,
                    )
                )
