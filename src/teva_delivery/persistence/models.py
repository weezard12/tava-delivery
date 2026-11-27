"""Tables. Every meaningful warehouse action is a row, written before the screen is told.

Konimbo stays the source of the commercial order; ``orders`` keeps the projection the
warehouse needs (no payment card data is ever requested or stored). Packing progress, the
event log, shipments, label prints and background jobs belong to this service.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """Stores UTC and always returns an aware UTC datetime, on SQLite and PostgreSQL alike."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not stored.")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


ID = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UtcDateTime(),
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class Order(Base):
    """A store order the warehouse handles."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    fulfillment_id: Mapped[int | None] = mapped_column(
        ForeignKey("fulfillments.id"), index=True, default=None
    )
    store_created_at: Mapped[datetime]
    imported_at: Mapped[datetime]
    synced_at: Mapped[datetime]
    status_title: Mapped[str | None] = mapped_column(String(200))
    payment_status: Mapped[str | None] = mapped_column(String(200))
    customer_name: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(320))
    address: Mapped[str | None] = mapped_column(String(600))
    phone_key: Mapped[str | None] = mapped_column(String(30), index=True)
    email_key: Mapped[str | None] = mapped_column(String(320), index=True)
    address_key: Mapped[str | None] = mapped_column(String(600), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    total_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    shipping_id: Mapped[str | None] = mapped_column(String(60))
    shipping_title: Mapped[str | None] = mapped_column(String(300))
    items_signature: Mapped[str] = mapped_column(Text, default="")
    # The category status the order had when it was imported, to restore after a detour
    # (awaiting inventory) and to keep its category while it carries the service's own status.
    origin_status: Mapped[str | None] = mapped_column(String(200), default=None)
    # Whether the order is still in one of the statuses the warehouse imports.
    in_scope: Mapped[bool] = mapped_column(Boolean, default=True)

    fulfillment: Mapped[Fulfillment | None] = relationship(back_populates="orders")


class Fulfillment(Base):
    """A packing unit: one order, or several the employee chose to send in one box."""

    __tablename__ = "fulfillments"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    primary_order_id: Mapped[str] = mapped_column(String(40))
    queued_at: Mapped[datetime]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(Integer, default=1)
    shipping_profile: Mapped[str] = mapped_column(String(20))
    review_reason: Mapped[str | None] = mapped_column(String(40), default=None)
    assigned_to: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(default=None)
    skip_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("fulfillments.id"), default=None)
    upstream_warning: Mapped[str | None] = mapped_column(String(60), default=None)
    packed_at: Mapped[datetime | None] = mapped_column(default=None)
    packed_by: Mapped[str | None] = mapped_column(String(256), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    # Set once the "packing started" status was sent, so it is sent once.
    preparing_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # A question the voice assistant asked and is waiting on.
    clarification: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    clarification_until: Mapped[datetime | None] = mapped_column(default=None)

    orders: Mapped[list[Order]] = relationship(back_populates="fulfillment")
    lines: Mapped[list[Line]] = relationship(
        back_populates="fulfillment", order_by="Line.position", cascade="all, delete-orphan"
    )
    notes: Mapped[list[Note]] = relationship(
        back_populates="fulfillment", order_by="Note.position", cascade="all, delete-orphan"
    )


class Line(Base):
    __tablename__ = "lines"
    __table_args__ = (UniqueConstraint("order_id", "store_key"),)

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    store_key: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str | None] = mapped_column(String(40), index=True)
    sku: Mapped[str | None] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(500))
    required: Mapped[int] = mapped_column(Integer)
    confirmed: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)
    missing_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    # Gone from the store order after a re-import; kept so confirmations are not lost.
    removed: Mapped[bool] = mapped_column(Boolean, default=False)

    fulfillment: Mapped[Fulfillment] = relationship(back_populates="lines")


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("order_id", "store_key"),)

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    store_key: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    confirmed_by: Mapped[str | None] = mapped_column(String(256), default=None)

    fulfillment: Mapped[Fulfillment] = relationship(back_populates="notes")


class PackingEvent(Base):
    """The packing log. Progress can be rebuilt from it; undo reverts its entries."""

    __tablename__ = "packing_events"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))
    line_id: Mapped[int | None] = mapped_column(ForeignKey("lines.id"), default=None)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id"), default=None)
    quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    reason: Mapped[str | None] = mapped_column(String(500), default=None)
    utterance_id: Mapped[str | None] = mapped_column(String(80), default=None)
    reverts_id: Mapped[int | None] = mapped_column(ForeignKey("packing_events.id"), default=None)
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)


class Transition(Base):
    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(256))
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(200), default=None)


class ActionRequest(Base):
    """One request per idempotency key: a repeated request gets the first answer back."""

    __tablename__ = "action_requests"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor: Mapped[str] = mapped_column(String(256))
    fulfillment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(60))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(index=True)


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(20), default="cargo")
    # Who made it: this service (cargo), Konimbo's integration (konimbo) or a person (manual).
    source: Mapped[str] = mapped_column(String(20), default="cargo")
    state: Mapped[str] = mapped_column(String(20), index=True)
    external_id: Mapped[str | None] = mapped_column(String(40), unique=True, default=None)
    customer_code: Mapped[int | None] = mapped_column(Integer, default=None)
    box: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    status_text: Mapped[str | None] = mapped_column(String(200), default=None)
    line_text: Mapped[str | None] = mapped_column(String(200), default=None)
    # The label link carries an access token: never logged and never sent to the browser.
    label_url: Mapped[str | None] = mapped_column(Text, default=None)
    error_code: Mapped[str | None] = mapped_column(String(60), default=None)
    job_id: Mapped[int | None] = mapped_column(Integer, default=None)
    requested_by: Mapped[str] = mapped_column(String(256))
    dispatched_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    status_synced_at: Mapped[datetime | None] = mapped_column(default=None)


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("fulfillments.id"), index=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[str] = mapped_column(String(20))
    printer_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(20))
    printer_job_id: Mapped[str | None] = mapped_column(String(200), default=None)
    warning: Mapped[str | None] = mapped_column(String(60), default=None)
    error_code: Mapped[str | None] = mapped_column(String(60), default=None)
    requested_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime]


class Job(Base):
    """The outbox: an external write, recorded in the same transaction as its cause."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_due", "state", "run_after"),)

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    fulfillment_id: Mapped[int | None] = mapped_column(
        ForeignKey("fulfillments.id"), index=True, default=None
    )
    order_id: Mapped[str | None] = mapped_column(String(40), default=None)
    payload: Mapped[dict[str, Any]]
    state: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime]
    lease_until: Mapped[datetime | None] = mapped_column(default=None)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(60), default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class DuplicateReview(Base):
    __tablename__ = "duplicate_reviews"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(String(600), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), index=True)
    blocking: Mapped[bool] = mapped_column(Boolean)
    order_ids: Mapped[list[Any]]
    suspects: Mapped[list[Any]]
    reasons: Mapped[list[Any]]
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_by: Mapped[str | None] = mapped_column(String(256), default=None)
    decision_note: Mapped[str | None] = mapped_column(String(500), default=None)


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("item_key", "alias"),)

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    # "item:<id>" or "sku:<code>": aliases follow the product into every order.
    item_key: Mapped[str] = mapped_column(String(160), index=True)
    alias: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime]


class Product(Base):
    """Product details fetched from the store for the screen (images)."""

    __tablename__ = "products"

    item_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    image_url: Mapped[str | None] = mapped_column(Text, default=None)
    fetched_at: Mapped[datetime | None] = mapped_column(default=None)
    found: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkSession(Base):
    """Where each employee is: the current order, the way back, and an optional batch."""

    __tablename__ = "work_sessions"

    actor: Mapped[str] = mapped_column(String(256), primary_key=True)
    current_id: Mapped[int | None] = mapped_column(Integer, default=None)
    history: Mapped[list[Any]] = mapped_column(default=list)
    category: Mapped[str | None] = mapped_column(String(30), default=None)
    batch: Mapped[list[Any]] = mapped_column(default=list)
    updated_at: Mapped[datetime]


class Utterance(Base):
    __tablename__ = "utterances"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor: Mapped[str] = mapped_column(String(256))
    fulfillment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    transcript: Mapped[str] = mapped_column(Text)
    normalized: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(default=None)
    outcome: Mapped[str] = mapped_column(String(30))
    response: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(index=True)


class AuditEvent(Base):
    """Who did what. Details hold IDs, codes and counts, never customer data."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(60))
    fulfillment_id: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    outcome: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(String(60), default=None)
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RuntimeSetting(Base):
    """Settings an admin changes on the settings screen (status names, rules)."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime]
    updated_by: Mapped[str] = mapped_column(String(256))


class StatusOption(Base):
    """A Konimbo order status name seen in the store, offered on the settings screen."""

    __tablename__ = "status_options"

    title: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_seen_at: Mapped[datetime]
    orders_seen: Mapped[int] = mapped_column(Integer, default=0)
