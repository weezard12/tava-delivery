"""The vocabulary of the warehouse: categories, states and kinds, stored as their string values."""

from enum import StrEnum


class Category(StrEnum):
    """The warehouse categories, as the office sorts orders in Konimbo today."""

    AWAITING_INVENTORY = "awaiting_inventory"  # ממתין למלאי
    CONSOLIDATION = "consolidation"  # איחוד הזמנות
    URGENT = "urgent"  # מיוחדים דחופים
    PICKUP_POINTS = "pickup_points"  # נקודות איסוף
    REGULAR = "regular"


# The default queue order. Orders awaiting inventory never enter the queue.
QUEUE_CATEGORIES = (
    Category.CONSOLIDATION,
    Category.URGENT,
    Category.PICKUP_POINTS,
    Category.REGULAR,
)

CATEGORY_LABELS = {
    Category.AWAITING_INVENTORY: "ממתין למלאי",
    Category.CONSOLIDATION: "איחוד הזמנות",
    Category.URGENT: "מיוחדים דחופים",
    Category.PICKUP_POINTS: "נקודות איסוף",
    Category.REGULAR: "משלוחים רגילים",
}


class FulfillmentState(StrEnum):
    """Where a packing unit (one order, or several combined) stands."""

    AWAITING_INVENTORY = "awaiting_inventory"  # out of the queue until stock arrives
    REVIEW = "review"  # out of the queue until a person decides (payment, duplicates)
    READY = "ready"  # in the queue
    PACKING = "packing"  # claimed by one employee
    PAUSED = "paused"  # progress kept, back for anyone to resume
    SKIPPED = "skipped"  # set aside with a reason, resumed by hand
    PACKED = "packed"  # everything confirmed; shipment, label and Konimbo still finishing
    COMPLETED = "completed"  # Konimbo confirmed ממתין לאיסוף
    CANCELLED = "cancelled"  # left the warehouse's scope, merged, or marked a duplicate


OPEN_STATES = frozenset(
    {
        FulfillmentState.READY,
        FulfillmentState.PACKING,
        FulfillmentState.PAUSED,
        FulfillmentState.SKIPPED,
    }
)
# States whose packing progress can still change.
EDITABLE_STATES = frozenset({FulfillmentState.PACKING})
INCOMPLETE_STATES = frozenset(
    {
        FulfillmentState.PAUSED,
        FulfillmentState.SKIPPED,
        FulfillmentState.AWAITING_INVENTORY,
        FulfillmentState.REVIEW,
    }
)
FINISHED_STATES = frozenset({FulfillmentState.COMPLETED, FulfillmentState.CANCELLED})


class ReviewReason(StrEnum):
    PAYMENT_UNVERIFIED = "payment_unverified"
    PAYMENT_FAILED = "payment_failed"
    DUPLICATE = "duplicate"


class ShippingProfile(StrEnum):
    EXPRESS = "express"  # Cargo courier to the customer's door
    BOX = "box"  # Cargo BOX pickup point
    NONE = "none"  # no courier, for example self pickup


class LineKind(StrEnum):
    PRODUCT = "product"
    UPGRADE = "upgrade"


class LineStatus(StrEnum):
    """The colour of a product row: neutral, orange, green or red."""

    UNCONFIRMED = "unconfirmed"
    PARTIAL = "partial"
    CONFIRMED = "confirmed"
    MISSING = "missing"


class NoteKind(StrEnum):
    PACKING = "packing"  # something to do in the box; must be confirmed
    DELIVERY = "delivery"  # for the courier; sent with the shipment
    INFO = "info"  # shown, nothing to confirm


class NoteSource(StrEnum):
    CUSTOMER = "customer"  # the order's customer note
    OFFICE = "office"  # a comment the office wrote in the Konimbo order history
    WAREHOUSE = "warehouse"  # written here


class ActionSource(StrEnum):
    MANUAL = "manual"
    VOICE = "voice"
    SYSTEM = "system"


class Action(StrEnum):
    """The validated actions. The voice assistant can only ask for these."""

    CONFIRM_PRODUCT = "confirm_product"
    UNDO_PRODUCT_CONFIRMATION = "undo_product_confirmation"
    GET_REMAINING_PRODUCTS = "get_remaining_products"
    READ_ORDER_NOTES = "read_order_notes"
    CONFIRM_ORDER_NOTE = "confirm_order_note"
    UNCONFIRM_ORDER_NOTE = "unconfirm_order_note"
    SET_NOTE_KIND = "set_note_kind"
    REPORT_MISSING_INVENTORY = "report_missing_inventory"
    UNDO_MISSING_INVENTORY = "undo_missing_inventory"
    COMPLETE_ORDER = "complete_order"
    REOPEN_ORDER = "reopen_order"
    PAUSE_ORDER = "pause_order"
    SKIP_ORDER = "skip_order"
    NEXT_ORDER = "next_order"
    PREVIOUS_ORDER = "previous_order"
    PRINT_SHIPPING_LABEL = "print_shipping_label"


class EventKind(StrEnum):
    """Entries of a packing unit's event log, from which progress and undo are derived."""

    CLAIMED = "claimed"
    RELEASED = "released"
    LINE_CONFIRMED = "line_confirmed"
    LINE_UNCONFIRMED = "line_unconfirmed"
    NOTE_CONFIRMED = "note_confirmed"
    NOTE_UNCONFIRMED = "note_unconfirmed"
    NOTE_KIND_CHANGED = "note_kind_changed"
    MISSING_REPORTED = "missing_reported"
    MISSING_CLEARED = "missing_cleared"
    PAUSED = "paused"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    REOPENED = "reopened"
    INVENTORY_RESOLVED = "inventory_resolved"
    REVIEW_RESOLVED = "review_resolved"
    MERGED = "merged"
    UPSTREAM_CHANGED = "upstream_changed"


class JobKind(StrEnum):
    KONIMBO_STATUS = "konimbo.status"
    KONIMBO_NOTE = "konimbo.note"
    # Look for the shipment number Konimbo's Cargo integration wrote into the order.
    KONIMBO_FIND_SHIPMENT = "konimbo.find_shipment"
    CARGO_CREATE_SHIPMENT = "cargo.create_shipment"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # stopped retrying; a person can retry it
    UNCERTAIN = "uncertain"  # the outcome is unknown; never retried without reconciliation
    CANCELLED = "cancelled"


ACTIVE_JOB_STATES = frozenset({JobState.PENDING, JobState.RUNNING})


class ShipmentState(StrEnum):
    REQUESTED = "requested"  # the attempt is recorded and may be on its way to Cargo
    CREATED = "created"  # Cargo answered with a shipment number
    LINKED = "linked"  # an existing Cargo shipment: found in the Konimbo order, or typed in
    UNCERTAIN = "uncertain"  # Cargo may or may not have created it
    FAILED = "failed"  # Cargo refused; nothing was created
    CANCELLED = "cancelled"


LIVE_SHIPMENT_STATES = frozenset(
    {ShipmentState.REQUESTED, ShipmentState.CREATED, ShipmentState.LINKED, ShipmentState.UNCERTAIN}
)
USABLE_SHIPMENT_STATES = frozenset({ShipmentState.CREATED, ShipmentState.LINKED})


class ShipmentSource(StrEnum):
    CARGO = "cargo"  # created here through Cargo's API
    KONIMBO = "konimbo"  # created by Konimbo's Cargo integration, number read from the order
    MANUAL = "manual"  # typed in by an employee


class PrintMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    REPRINT = "reprint"


class PrintState(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class DuplicateKind(StrEnum):
    ACCIDENTAL_DUPLICATE = "accidental_duplicate"
    FAILED_PAYMENT_ATTEMPT = "failed_payment_attempt"
    CONSOLIDATION_CANDIDATE = "consolidation_candidate"
    INTENTIONAL_MULTIPLE = "intentional_multiple"
    ALREADY_FULFILLED = "already_fulfilled"


# Kinds that hold the orders out of the queue until a person decides.
BLOCKING_DUPLICATE_KINDS = frozenset(
    {DuplicateKind.ACCIDENTAL_DUPLICATE, DuplicateKind.CONSOLIDATION_CANDIDATE}
)


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CONSOLIDATED = "consolidated"
    KEPT_SEPARATE = "kept_separate"
    DUPLICATE_MARKED = "duplicate_marked"
    DISMISSED = "dismissed"  # the orders left the queue before anyone decided


class ReviewDecision(StrEnum):
    CONSOLIDATE = "consolidate"
    KEEP_SEPARATE = "keep_separate"
    MARK_DUPLICATE = "mark_duplicate"
