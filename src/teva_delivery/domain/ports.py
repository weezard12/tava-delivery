"""What the services need from the outside world, independent of any provider.

Konimbo, Cargo, teva-printer, speech recognition and the AI interpreter each implement one of
these protocols. Demo implementations live in ``providers``; tests pass their own fakes.
Provider failures are ``ProviderError`` with a stable code and whether a write may have
happened.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from .enums import LineKind


@dataclass(frozen=True, slots=True)
class StoreLine:
    """One product row of a store order."""

    key: str  # stable within the order, so a re-import updates the same row
    item_id: str | None
    sku: str | None
    title: str
    quantity: int
    kind: LineKind = LineKind.PRODUCT


@dataclass(frozen=True, slots=True)
class StoreStatus:
    """One entry of a store order's status history."""

    title: str
    comment: str | None
    username: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoreOrder:
    id: str
    created_at: datetime
    status_title: str | None
    payment_status: str | None
    customer_name: str | None
    email: str | None
    phone: str | None
    address: str | None
    note: str | None
    total_price: Decimal | None
    shipping_id: str | None
    shipping_title: str | None
    lines: tuple[StoreLine, ...]
    statuses: tuple[StoreStatus, ...] = ()
    # Other order fields that may carry the courier's shipment number (tracking fields).
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductInfo:
    item_id: str
    title: str | None
    image_url: str | None


class OrderStore(Protocol):
    """The store (Konimbo). Reads are safe to repeat; ``append_status`` is not."""

    @property
    def requests_left(self) -> int: ...

    def list_orders(
        self, status: str | None, created_after: datetime, *, limit: int | None = None
    ) -> Iterator[StoreOrder]:
        """Orders created after a time, in one status (or any status when ``None``)."""
        ...

    def get_order(self, order_id: str) -> StoreOrder: ...

    def append_status(
        self, order_id: str, *, title: str, comment: str, username: str
    ) -> StoreOrder: ...

    def get_product(self, item_id: str) -> ProductInfo | None: ...


@dataclass(frozen=True, slots=True)
class PostalAddress:
    name: str
    street: str
    city: str
    phone: str
    email: str = ""
    floor: str = ""
    apartment: str = ""
    company: str = ""


@dataclass(frozen=True, slots=True)
class ShipmentRequest:
    reference: str  # the store order number, sent as Cargo's order and transaction ID
    customer_code: int
    box: bool
    recipient: PostalAddress
    sender: PostalAddress
    total_value: Decimal
    notes: str
    parcels: int = 1
    box_point_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedShipment:
    external_id: str
    status_code: int | None
    status_text: str | None
    line_text: str | None


@dataclass(frozen=True, slots=True)
class ShipmentStatus:
    code: int | None
    text: str | None


class Carrier(Protocol):
    """The courier (Cargo). ``create_shipment`` is never idempotent on the provider's side."""

    def create_shipment(self, request: ShipmentRequest) -> CreatedShipment: ...

    def get_status(self, external_id: str, customer_code: int) -> ShipmentStatus: ...

    def label_url(self, external_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class PrintCredentials:
    """How to authenticate one print: the employee's workspace session, passed on."""

    cookie_name: str | None = None
    session: str | None = None
    origin: str | None = None
    csrf_token: str | None = None


@dataclass(frozen=True, slots=True)
class PrintReceipt:
    job_id: str
    shipment: str | None
    warning: str | None
    job_state: str | None


class LabelPrinter(Protocol):
    def print_label(
        self,
        label_url: str,
        *,
        printer_id: str,
        direct: bool,
        credentials: PrintCredentials,
    ) -> PrintReceipt: ...

    def printers(self, credentials: PrintCredentials) -> list[dict[str, Any]]:
        """The printers on offer, as ``{"id", "label"}``, for the settings screen."""
        ...


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    confidence: float | None
    duration_seconds: float | None = None
    language: str | None = None


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, content_type: str) -> Transcript: ...


@dataclass(frozen=True, slots=True)
class InterpreterProduct:
    id: int
    name: str
    required: int
    confirmed: int


@dataclass(frozen=True, slots=True)
class InterpreterNote:
    id: int
    text: str
    kind: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class InterpreterContext:
    products: Sequence[InterpreterProduct]
    notes: Sequence[InterpreterNote]


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """An action an AI model proposed. It is validated like any manual request."""

    action: str
    product_id: int | None = None
    note_id: int | None = None
    quantity: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Interpretation:
    actions: tuple[ProposedAction, ...] = ()
    # The model's own judgement that the sentence was not meant for the system.
    unrelated: bool = False
    clarification: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class Interpreter(Protocol):
    @property
    def name(self) -> str: ...

    def interpret(self, transcript: str, context: InterpreterContext) -> Interpretation: ...


type ProviderMode = Literal["demo", "live", "off"]
