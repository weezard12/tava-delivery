"""Warehouse settings that the office changes at run time: Konimbo status names and rules.

The environment gives the defaults; an admin changes them on the settings screen and they are
stored in the database, so a change takes effect without a deploy. Konimbo has no endpoint
that lists its order statuses, so the screen offers the status names seen in recent orders.
"""

import re
import threading
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from ..core.config import DEFAULT_SHIPPING_RULES, Settings, ShippingRule, split_list
from ..domain.enums import Category, ShippingProfile
from ..domain.errors import ServiceError
from ..domain.hebrew.text import normalize
from ..persistence.database import Database
from ..persistence.models import RuntimeSetting

SETTINGS_KEY = "warehouse"
DEFAULT_SHIPMENT_PATTERN = (
    r"(?i)(?:cargo|קארגו|קרגו|משלוח|shipment|tracking|מעקב)\D{0,30}?(\d{6,12})"
)


# Demo mode only, when unset: the status that sends an order to Cargo, and the label printer.
DEMO_TRANSMIT_STATUS = "הועבר ל-Cargo"
DEMO_PRINTER_ID = "demo-label"


class WarehouseConfig(BaseModel):
    """Everything here is safe to show to any signed-in employee."""

    status_awaiting_inventory: str = Field(min_length=1, max_length=200)
    status_consolidation: str = Field(min_length=1, max_length=200)
    status_urgent: str = Field(min_length=1, max_length=200)
    status_pickup_points: str = Field(min_length=1, max_length=200)
    status_regular: list[str] = Field(min_length=1, max_length=10)
    # Optional: set when packing starts, so the office sees the warehouse has the order.
    status_preparing: str = Field(default="", max_length=200)
    # The status whose change makes Konimbo's Cargo integration send the order to Cargo. It
    # may be the awaiting pickup status itself.
    status_transmit: str = Field(default="", max_length=200)
    # When to set it: when packing starts (the label is ready when packing ends), when packing
    # ends, or never (the office keeps doing it).
    transmit_on: Literal["claim", "complete", "off"] = "complete"
    status_awaiting_pickup: str = Field(min_length=1, max_length=200)
    note_title: str = Field(min_length=1, max_length=200)
    paid_statuses: list[str] = Field(default_factory=list, max_length=20)
    failed_payment_statuses: list[str] = Field(default_factory=list, max_length=20)
    # konimbo: Konimbo's integration creates the shipment; the service finds its number.
    # cargo: the service creates the shipment through Cargo's API itself.
    shipment_source: Literal["konimbo", "cargo"] = "konimbo"
    shipment_number_pattern: str = Field(default=DEFAULT_SHIPMENT_PATTERN, max_length=300)
    shipping_rules: list[ShippingRule] = Field(
        default_factory=lambda: list(DEFAULT_SHIPPING_RULES), max_length=30
    )
    auto_print: bool = False
    require_label_print: bool = True
    label_printer_id: str = Field(default="", max_length=128)

    @field_validator("status_regular", "paid_statuses", "failed_payment_statuses", mode="before")
    @classmethod
    def split(cls, value: object) -> object:
        return list(split_list(value)) if isinstance(value, str) else value

    @field_validator("shipment_number_pattern")
    @classmethod
    def pattern(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error:
            raise ValueError("shipment_number_pattern is not a valid expression.") from None
        if compiled.groups < 1:
            raise ValueError("shipment_number_pattern needs one group around the number.")
        return value

    @model_validator(mode="after")
    def distinct(self) -> WarehouseConfig:
        names = [
            self.status_awaiting_inventory,
            self.status_consolidation,
            self.status_urgent,
            self.status_pickup_points,
            *self.status_regular,
            self.status_awaiting_pickup,
        ]
        names = [name.strip() for name in names]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Each Konimbo status may be used for one purpose only.")
        if self.note_title.strip() in {*names, self.status_preparing, self.status_transmit}:
            raise ValueError(
                "The warehouse note title must not be an order status, or every note would "
                "move the order."
            )
        if self.transmits_at_pickup and self.transmit_on == "claim":
            raise ValueError(
                "When the awaiting pickup status sends the order to Cargo, it is set when "
                "packing ends, not when it starts."
            )
        if self.status_preparing and self.status_preparing in names:
            raise ValueError("The packing started status must not be one of the categories.")
        if self.status_transmit and self.status_transmit in names[:-1]:
            raise ValueError("The status that sends to Cargo must not be one of the categories.")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> WarehouseConfig:
        demo = settings.mode == "demo"
        return cls(
            status_awaiting_inventory=settings.konimbo_status_awaiting_inventory,
            status_consolidation=settings.konimbo_status_consolidation,
            status_urgent=settings.konimbo_status_urgent,
            status_pickup_points=settings.konimbo_status_pickup_points,
            status_regular=list(split_list(settings.konimbo_status_regular)),
            status_preparing=settings.konimbo_status_preparing,
            status_transmit=settings.konimbo_status_transmit
            or (DEMO_TRANSMIT_STATUS if demo else ""),
            transmit_on=settings.transmit_on,
            status_awaiting_pickup=settings.konimbo_status_awaiting_pickup,
            note_title=settings.konimbo_note_title,
            paid_statuses=list(split_list(settings.konimbo_paid_statuses)),
            failed_payment_statuses=list(split_list(settings.konimbo_failed_payment_statuses)),
            shipment_source=settings.shipment_source,
            shipment_number_pattern=settings.shipment_number_pattern,
            shipping_rules=list(settings.shipping_rules),
            auto_print=settings.auto_print,
            require_label_print=settings.require_label_print,
            label_printer_id=settings.label_printer_id or (DEMO_PRINTER_ID if demo else ""),
        )

    # Derived lookups.

    @property
    def category_by_status(self) -> dict[str, Category]:
        mapping = {
            self.status_awaiting_inventory: Category.AWAITING_INVENTORY,
            self.status_consolidation: Category.CONSOLIDATION,
            self.status_urgent: Category.URGENT,
            self.status_pickup_points: Category.PICKUP_POINTS,
        }
        mapping.update(dict.fromkeys(self.status_regular, Category.REGULAR))
        return mapping

    @property
    def transmits_at_pickup(self) -> bool:
        """The awaiting pickup status is also the one that sends the order to Cargo."""
        return bool(self.status_transmit) and self.status_transmit == self.status_awaiting_pickup

    @property
    def transmit_status(self) -> str | None:
        """The status the service sets to send the order to Cargo, apart from the final one."""
        if not self.status_transmit or self.transmit_on == "off" or self.transmits_at_pickup:
            return None
        return self.status_transmit

    @property
    def own_statuses(self) -> frozenset[str]:
        """Statuses the service itself sets while the order is still the warehouse's."""
        return frozenset(
            status
            for status in (self.status_preparing, self.status_transmit)
            if status and status != self.status_awaiting_pickup
        )

    @property
    def tracked_statuses(self) -> tuple[str, ...]:
        """The statuses the import reads: the categories plus the warehouse's own ones."""
        statuses = list(self.category_by_status)
        for extra in (self.status_preparing, self.status_transmit):
            if extra and extra not in statuses and extra != self.status_awaiting_pickup:
                statuses.append(extra)
        return tuple(statuses)

    def shipment_numbers(self, text: str) -> list[str]:
        return [match.group(1) for match in re.finditer(self.shipment_number_pattern, text)]

    def shipping_profile(self, category: Category, shipping_title: str | None) -> ShippingProfile:
        title = normalize(shipping_title or "")
        for rule in self.shipping_rules:
            if rule.category is not None and rule.category is not category:
                continue
            if rule.match is not None and normalize(rule.match) not in title:
                continue
            return rule.profile
        return ShippingProfile.EXPRESS


class RuntimeConfig:
    """Reads the warehouse settings once and again whenever an admin saves them."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._lock = threading.Lock()
        self._cached: WarehouseConfig | None = None
        self.defaults = WarehouseConfig.from_settings(settings)

    def current(self) -> WarehouseConfig:
        with self._lock:
            if self._cached is not None:
                return self._cached
        with self.db.session() as session:
            row = session.get(RuntimeSetting, SETTINGS_KEY)
            stored = dict(row.value) if row is not None else {}
        merged = {**self.defaults.model_dump(), **stored}
        try:
            config = WarehouseConfig.model_validate(merged)
        except ValidationError:
            # A stored value no longer validates (for example after an upgrade): use defaults.
            config = self.defaults
        with self._lock:
            self._cached = config
        return config

    def update(self, changes: dict[str, Any], *, actor: str, now: datetime) -> WarehouseConfig:
        merged = {**self.current().model_dump(), **changes}
        try:
            config = WarehouseConfig.model_validate(merged)
        except ValidationError as error:
            first = error.errors()[0]
            raise ServiceError("settings_invalid", message=_message(first)) from None
        stored = config.model_dump(mode="json")
        with self.db.session(write=True) as session:
            row = session.get(RuntimeSetting, SETTINGS_KEY, with_for_update=True)
            if row is None:
                session.add(
                    RuntimeSetting(key=SETTINGS_KEY, value=stored, updated_at=now, updated_by=actor)
                )
            else:
                row.value = stored
                row.updated_at = now
                row.updated_by = actor
        with self._lock:
            self._cached = config
        return config

    def updated(self) -> tuple[datetime, str] | None:
        with self.db.session() as session:
            row = session.scalars(
                select(RuntimeSetting).where(RuntimeSetting.key == SETTINGS_KEY)
            ).one_or_none()
            return (row.updated_at, row.updated_by) if row is not None else None


def _message(error: Any) -> str:
    text = str(error.get("msg", ""))
    if text.startswith("Value error, "):
        text = text.removeprefix("Value error, ")
    return f"ההגדרות לא נשמרו: {text}"
