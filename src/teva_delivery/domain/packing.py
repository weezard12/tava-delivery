"""Pure packing rules: row colours, progress, what still blocks completion, and transitions."""

from collections.abc import Iterable
from dataclasses import dataclass

from .enums import FulfillmentState, LineStatus, NoteKind
from .errors import ServiceError


@dataclass(frozen=True, slots=True)
class LineFacts:
    id: int
    title: str
    required: int
    confirmed: int
    missing: int

    @property
    def remaining(self) -> int:
        return max(self.required - self.confirmed, 0)

    @property
    def status(self) -> LineStatus:
        return line_status(self.required, self.confirmed, self.missing)


@dataclass(frozen=True, slots=True)
class NoteFacts:
    id: int
    kind: NoteKind
    confirmed: bool

    @property
    def open(self) -> bool:
        return self.kind is NoteKind.PACKING and not self.confirmed


def line_status(required: int, confirmed: int, missing: int) -> LineStatus:
    if missing > 0:
        return LineStatus.MISSING
    if confirmed >= required:
        return LineStatus.CONFIRMED
    if confirmed > 0:
        return LineStatus.PARTIAL
    return LineStatus.UNCONFIRMED


@dataclass(frozen=True, slots=True)
class Progress:
    units_confirmed: int
    units_required: int
    lines_confirmed: int
    lines_total: int
    notes_open: int
    lines_missing: int

    @property
    def complete(self) -> bool:
        return (
            self.lines_missing == 0
            and self.lines_confirmed == self.lines_total
            and self.notes_open == 0
        )


def progress(lines: Iterable[LineFacts], notes: Iterable[NoteFacts]) -> Progress:
    rows = list(lines)
    return Progress(
        units_confirmed=sum(min(row.confirmed, row.required) for row in rows),
        units_required=sum(row.required for row in rows),
        lines_confirmed=sum(1 for row in rows if row.status is LineStatus.CONFIRMED),
        lines_total=len(rows),
        notes_open=sum(1 for note in notes if note.open),
        lines_missing=sum(1 for row in rows if row.status is LineStatus.MISSING),
    )


def check_quantity(line: LineFacts, quantity: int) -> None:
    if quantity <= 0:
        raise ServiceError("quantity_invalid")
    if line.missing > 0:
        raise ServiceError("line_missing")
    if line.confirmed + quantity > line.required:
        raise ServiceError(
            "quantity_exceeds_required",
            message=(
                f"בהזמנה יש {line.required} יחידות של {line.title}, וכבר אושרו {line.confirmed}."
            ),
        )


# Which states each state may move to. Anything else is a bug or a stale screen.
TRANSITIONS: dict[FulfillmentState, frozenset[FulfillmentState]] = {
    FulfillmentState.READY: frozenset(
        {
            FulfillmentState.PACKING,
            FulfillmentState.REVIEW,
            FulfillmentState.AWAITING_INVENTORY,
            FulfillmentState.CANCELLED,
        }
    ),
    FulfillmentState.REVIEW: frozenset(
        {FulfillmentState.READY, FulfillmentState.CANCELLED, FulfillmentState.AWAITING_INVENTORY}
    ),
    FulfillmentState.AWAITING_INVENTORY: frozenset(
        {
            FulfillmentState.READY,
            FulfillmentState.PACKING,
            FulfillmentState.REVIEW,
            FulfillmentState.CANCELLED,
        }
    ),
    FulfillmentState.PACKING: frozenset(
        {
            FulfillmentState.PAUSED,
            FulfillmentState.SKIPPED,
            FulfillmentState.AWAITING_INVENTORY,
            FulfillmentState.PACKED,
            FulfillmentState.READY,
            FulfillmentState.CANCELLED,
        }
    ),
    FulfillmentState.PAUSED: frozenset(
        {
            FulfillmentState.PACKING,
            FulfillmentState.REVIEW,
            FulfillmentState.AWAITING_INVENTORY,
            FulfillmentState.CANCELLED,
        }
    ),
    FulfillmentState.SKIPPED: frozenset(
        {
            FulfillmentState.PACKING,
            FulfillmentState.REVIEW,
            FulfillmentState.AWAITING_INVENTORY,
            FulfillmentState.CANCELLED,
        }
    ),
    FulfillmentState.PACKED: frozenset({FulfillmentState.COMPLETED, FulfillmentState.PACKING}),
    FulfillmentState.COMPLETED: frozenset(),
    FulfillmentState.CANCELLED: frozenset(),
}


def can_transition(current: FulfillmentState, target: FulfillmentState) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: FulfillmentState, target: FulfillmentState) -> None:
    if not can_transition(current, target):
        if current is FulfillmentState.COMPLETED:
            raise ServiceError("order_finished")
        raise ServiceError("invalid_state")
