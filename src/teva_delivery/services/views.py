"""Read models the API returns. They hold what the packing screen shows and nothing more:
the customer's name but never their phone, email, address or the Cargo label link."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.enums import (
    Category,
    FulfillmentState,
    JobState,
    LineStatus,
    NoteKind,
    NoteSource,
    PrintMode,
    PrintState,
    ShipmentState,
    ShippingProfile,
)


class LineView(BaseModel):
    id: int
    order_id: str
    title: str
    sku: str | None
    image_url: str | None
    kind: str
    required: int
    confirmed: int
    remaining: int
    missing: int
    missing_reason: str | None
    status: LineStatus
    removed: bool
    aliases: list[str] = Field(default_factory=list)


class NoteView(BaseModel):
    id: int
    order_id: str | None
    source: NoteSource
    kind: NoteKind
    text: str
    requires_confirmation: bool
    confirmed: bool
    confirmed_at: datetime | None


class OrderSummary(BaseModel):
    id: str
    customer_name: str | None
    payment_status: str | None
    payment_ok: bool | None
    shipping_title: str | None
    status_title: str | None
    created_at: datetime


class StepView(BaseModel):
    """One completion step: the shipment, the label, the store status."""

    key: Literal["shipment", "label", "store_status"]
    state: Literal[
        "done", "pending", "running", "waiting", "failed", "uncertain", "blocked", "skipped"
    ]
    code: str | None = None
    message: str | None = None
    dry_run: bool = False


class ShipmentView(BaseModel):
    id: int
    state: ShipmentState
    external_id: str | None
    box: bool
    status_code: int | None
    status_text: str | None
    line_text: str | None
    error_code: str | None
    error_message: str | None
    label_ready: bool
    created_at: datetime


class PrintView(BaseModel):
    id: int
    mode: PrintMode
    state: PrintState
    printer_id: str
    printer_job_id: str | None
    warning: str | None
    error_code: str | None
    created_at: datetime


class JobView(BaseModel):
    id: int
    kind: str
    state: JobState
    order_id: str | None
    attempts: int
    last_error: str | None
    last_error_message: str | None
    dry_run: bool
    purpose: str | None
    created_at: datetime
    updated_at: datetime
    retryable: bool


class Progress(BaseModel):
    units_confirmed: int
    units_required: int
    lines_confirmed: int
    lines_total: int
    notes_open: int
    lines_missing: int
    complete: bool


class Holder(BaseModel):
    actor: str
    mine: bool
    lease_until: datetime | None


class ClarificationView(BaseModel):
    question: str
    candidates: list[int]


class FulfillmentView(BaseModel):
    id: int
    version: int
    state: FulfillmentState
    category: Category
    category_label: str
    urgent: bool
    shipping_profile: ShippingProfile
    review_reason: str | None
    skip_reason: str | None
    upstream_warning: str | None
    orders: list[OrderSummary]
    lines: list[LineView]
    notes: list[NoteView]
    progress: Progress
    holder: Holder | None
    editable: bool
    shipment: ShipmentView | None
    prints: list[PrintView]
    steps: list[StepView]
    jobs: list[JobView]
    clarification: ClarificationView | None
    queued_at: datetime
    packed_at: datetime | None
    completed_at: datetime | None
    # Whether the screen should print the label now (automatic printing is on and due).
    auto_print_due: bool


class QueueItem(BaseModel):
    id: int
    state: FulfillmentState
    category: Category
    urgent: bool
    order_ids: list[str]
    customer_name: str | None
    lines: int
    units: int
    progress: Progress
    holder: Holder | None
    queued_at: datetime
    review_reason: str | None
    skip_reason: str | None
    problem: str | None


class QueueCount(BaseModel):
    category: Category
    label: str
    ready: int
    in_progress: int


class QueueView(BaseModel):
    counts: list[QueueCount]
    awaiting_inventory: int
    review: int
    items: list[QueueItem]


class Position(BaseModel):
    """Where the employee is: this order's place among the ones they can still reach."""

    index: int
    total: int
    remaining_in_queue: int
    can_go_back: bool
    batch: bool
    category: Category | None


class WorkView(BaseModel):
    fulfillment: FulfillmentView | None
    position: Position


class ActionResult(BaseModel):
    ok: bool
    action: str
    code: str | None = None
    message: str
    speech: str
    data: dict[str, Any] = Field(default_factory=dict)
    fulfillment: FulfillmentView | None = None
    # Navigation actions move the employee to another order.
    navigated_to: int | None = None


class VoiceResult(BaseModel):
    utterance_id: str
    transcript: str
    outcome: Literal[
        "executed", "ignored", "clarify", "failed", "duplicate", "low_confidence", "help"
    ]
    speech: str
    interpreter: Literal["rules", "ai", "none"]
    results: list[ActionResult]
    fulfillment: FulfillmentView | None = None
    navigated_to: int | None = None


class DuplicateOrderView(BaseModel):
    id: str
    fulfillment_id: int | None
    state: FulfillmentState | None
    customer_name: str | None
    created_at: datetime
    payment_status: str | None
    total_price: str | None
    items: list[str]
    masked_phone: str | None
    masked_email: str | None
    suspect: bool


class DuplicateReviewView(BaseModel):
    id: int
    kind: str
    status: str
    blocking: bool
    reasons: list[str]
    orders: list[DuplicateOrderView]
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None


class AliasView(BaseModel):
    id: int
    item_key: str
    alias: str
    created_at: datetime


class SyncRunView(BaseModel):
    kind: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    error: str | None
    counts: dict[str, Any]


class SyncView(BaseModel):
    runs: list[SyncRunView]
    failed_jobs: list[JobView]
    uncertain_jobs: list[JobView]
    pending_jobs: int
    konimbo_requests_left: int | None


class Readiness(BaseModel):
    ready: bool
    missing: list[str]
    writes: bool | None = None


class InfoView(BaseModel):
    mode: Literal["demo", "live"]
    version: str
    timezone: str
    konimbo: Readiness
    cargo: Readiness
    printer: Readiness
    speech: Readiness
    ai: Readiness
    auto_print: bool
    require_label_print: bool
    label_printer_id: str | None
    categories: dict[str, str]
    statuses: dict[str, str]
    voice_examples: list[str]
