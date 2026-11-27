"""The shared pieces every service uses: settings, the database, the clock and the providers."""

from dataclasses import dataclass, field
from datetime import datetime

from ..core.clock import Clock, utc_now
from ..core.config import Settings
from ..domain.ports import Carrier, Interpreter, LabelPrinter, OrderStore, Transcriber
from ..persistence.database import Database
from .events import EventBus
from .warehouse_config import RuntimeConfig, WarehouseConfig


@dataclass(slots=True)
class Providers:
    store: OrderStore | None = None
    carrier: Carrier | None = None
    printer: LabelPrinter | None = None
    transcriber: Transcriber | None = None
    interpreter: Interpreter | None = None


@dataclass(slots=True)
class Context:
    settings: Settings
    db: Database
    providers: Providers
    events: EventBus = field(default_factory=EventBus)
    clock: Clock = utc_now
    runtime: RuntimeConfig = field(init=False)

    def __post_init__(self) -> None:
        self.runtime = RuntimeConfig(self.settings, self.db)

    def now(self) -> datetime:
        return self.clock()

    def config(self) -> WarehouseConfig:
        """The warehouse settings in force: environment defaults with the admin's changes."""
        return self.runtime.current()

    @property
    def demo(self) -> bool:
        return self.settings.mode == "demo"

    @property
    def store_dry_run(self) -> bool:
        """Konimbo writes are recorded but not sent. Demo mode writes to the demo store."""
        return not self.demo and not self.settings.konimbo_writes

    @property
    def can_create_shipments(self) -> bool:
        """Whether this service may create Cargo shipments itself (the ``cargo`` source)."""
        return (
            self.config().shipment_source == "cargo"
            and self.providers.carrier is not None
            and (self.demo or self.settings.cargo_writes)
        )

    @property
    def can_print_labels(self) -> bool:
        """Cargo labels need Cargo (for the label link) and teva-printer."""
        return (
            self.providers.carrier is not None
            and self.providers.printer is not None
            and bool(self.config().label_printer_id)
        )
