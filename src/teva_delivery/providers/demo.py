"""Demo providers: a small Konimbo store, Cargo and a label printer that live in memory.

Demo mode runs the whole flow with nothing leaving the process. The store keeps its orders in
Konimbo's own JSON shape and reads them with the real parser. It also plays Konimbo's Cargo
integration: when an order gets the status that sends it to Cargo, a status comment with a
shipment number appears in its history, as the real integration is expected to write it.
The customers are invented.
"""

import itertools
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..core.clock import Clock
from ..domain.errors import ProviderError
from ..domain.ports import (
    CreatedShipment,
    PrintCredentials,
    PrintReceipt,
    ProductInfo,
    ShipmentRequest,
    ShipmentStatus,
    StoreOrder,
)
from ..services.warehouse_config import DEMO_PRINTER_ID, WarehouseConfig
from .konimbo import parse_order

SELF_PICKUP = "איסוף עצמי"

PRODUCTS: dict[str, tuple[str, str]] = {
    "1001": ("סרום שפע לשיער 50 מ״ל", "SRM-50"),
    "1002": ("סבון זית טבעי", "SOAP-OLV"),
    "1003": ("קרם ידיים לבנדר", "HND-LAV"),
    "1004": ("שמן ארגן אורגני 100 מ״ל", "ARG-100"),
    "1005": ("סרום ויטמין C כתום", "SRM-VTC"),
    "1006": ("תה צמחים מרגיע", "TEA-CLM"),
    "1007": ("משחת שיניים מנטה", "TP-MNT"),
    "1008": ("ספוג ליפה טבעי", "LUFA"),
    "1009": ("מגבון פנים במבוק", "BMB-WIPE"),
    "1010": ("שמפו מוצק רוזמרין", "SHMP-RSM"),
}

# (hours ago, status, payment, customer, phone, email, address, note, shipping, items)
ORDERS: tuple[tuple[Any, ...], ...] = (
    (
        30,
        "מיוחדים דחופים",
        "שולם",
        "דנה כהן",
        "050-1234567",
        "dana@example.com",
        "הרצל 12, תל אביב",
        "בבקשה לעטוף באריזת מתנה. להשאיר ליד הדלת",
        "שליח עד הבית",
        (("1001", 2), ("1002", 1)),
    ),
    (
        26,
        "הזמנה חדשה",
        "שולם",
        "יוסי לוי",
        "052-7654321",
        "yossi@example.com",
        "ז׳בוטינסקי 40, רמת גן",
        "",
        "שליח עד הבית",
        (("1003", 1), ("1004", 1), ("1007", 3)),
    ),
    (
        25,
        "הזמנה חדשה",
        "שולם",
        "מיכל אברהם",
        "054-1112233",
        "michal@example.com",
        "העצמאות 5, חיפה",
        "בלי שקית ניילון בבקשה",
        "שליח עד הבית",
        (("1005", 1), ("1008", 2)),
    ),
    (
        20,
        "נקודות איסוף",
        "שולם",
        "אורי שמש",
        "053-9998877",
        "ori@example.com",
        "הנביאים 3, ירושלים",
        "",
        "נקודת איסוף BOX",
        (("1006", 2), ("1010", 1)),
    ),
    (
        18,
        "הזמנה חדשה",
        "שולם",
        "רונית גל",
        "058-4445566",
        "ronit@example.com",
        "הגפן 8, כרכור",
        "",
        SELF_PICKUP,
        (("1009", 4),),
    ),
    # The same customer twice within an hour, with the same items: a duplicate to review.
    (
        12,
        "הזמנה חדשה",
        "שולם",
        "נועה פרץ",
        "050-3216549",
        "noa@example.com",
        "הדקל 17, באר שבע",
        "",
        "שליח עד הבית",
        (("1002", 2), ("1003", 1)),
    ),
    (
        11.5,
        "הזמנה חדשה",
        "שולם",
        "נועה פרץ",
        "050-3216549",
        "noa@example.com",
        "הדקל 17, באר שבע",
        "",
        "שליח עד הבית",
        (("1002", 2), ("1003", 1)),
    ),
    (
        6,
        "איחוד הזמנות",
        "שולם",
        "עמית ברק",
        "052-1239876",
        "amit@example.com",
        "הרימון 2, מודיעין",
        "לצרף את ההזמנה הקודמת של הלקוח",
        "שליח עד הבית",
        (("1001", 1), ("1005", 1), ("1004", 2)),
    ),
)


def _when(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class DemoStore:
    """Konimbo, in memory. ``config`` gives the statuses in force, as the real store has them."""

    def __init__(self, clock: Clock, config: Callable[[], WarehouseConfig]) -> None:
        self._clock = clock
        self._config = config
        self._lock = threading.Lock()
        self._shipments = itertools.count(51_000_001)
        # Whether the store plays Konimbo's Cargo integration (tests turn it off).
        self.cargo_integration = True
        self._orders: dict[str, dict[str, Any]] = {}
        now = clock()
        for number, row in enumerate(ORDERS, start=20_001):
            hours, status, payment, name, phone, email, address, note, shipping, items = row
            created = now - timedelta(hours=hours)
            self._orders[str(number)] = {
                "id": number,
                "created_at": _when(created),
                "status_option_title": status,
                "payment_status": payment,
                "name": name,
                "phone": phone,
                "email": email,
                "address": address,
                "note": note,
                "total_price": str(sum(quantity * 59 for _, quantity in items)),
                "shipping": {"id": 1 if shipping != SELF_PICKUP else 2, "title": shipping},
                "items": [
                    {
                        "id": int(item),
                        "title": PRODUCTS[item][0],
                        "code": PRODUCTS[item][1],
                        "quantity": quantity,
                        "price": "59",
                    }
                    for item, quantity in items
                ],
                "upgrades": [],
                "statuses": [
                    {
                        "status_option_title": status,
                        "username": "אתר",
                        "comment": "",
                        "created_at": _when(created),
                    }
                ],
            }

    @property
    def requests_left(self) -> int:
        return 100

    def list_orders(
        self, status: str | None, created_after: datetime, *, limit: int | None = None
    ) -> Iterator[StoreOrder]:
        with self._lock:
            orders = [parse_order(data) for data in self._orders.values()]
        found = [
            order
            for order in orders
            if order.created_at >= created_after
            and (status is None or order.status_title == status)
        ]
        found.sort(key=lambda order: order.created_at)
        yield from found[:limit] if limit else found

    def get_order(self, order_id: str) -> StoreOrder:
        with self._lock:
            data = self._orders.get(order_id)
            if data is None:
                raise ProviderError("konimbo_order_not_found", sent=False)
            return parse_order(data)

    def append_status(
        self, order_id: str, *, title: str, comment: str, username: str
    ) -> StoreOrder:
        config = self._config()
        with self._lock:
            data = self._orders.get(order_id)
            if data is None:
                raise ProviderError("konimbo_order_not_found", sent=False)
            now = self._clock()
            statuses: list[dict[str, Any]] = data["statuses"]
            statuses.append(
                {
                    "status_option_title": title,
                    "username": username,
                    "comment": comment,
                    "created_at": _when(now),
                }
            )
            if title != config.note_title:
                data["status_option_title"] = title
            trigger = config.status_transmit or config.status_awaiting_pickup
            ships = data["shipping"]["title"] != SELF_PICKUP
            already = any(entry.get("username") == "Cargo" for entry in statuses)
            integrated = self.cargo_integration and config.shipment_source == "konimbo"
            if integrated and title == trigger and ships and not already:
                statuses.append(
                    {
                        "status_option_title": title,
                        "username": "Cargo",
                        "comment": f"ההזמנה נשלחה ל-Cargo. מספר משלוח: {next(self._shipments)}",
                        "created_at": _when(now),
                    }
                )
            return parse_order(data)

    def edit(self, order_id: str, **fields: Any) -> None:
        """Change an order as the office would in Konimbo's admin (tests and demos)."""
        with self._lock:
            self._orders[order_id].update(fields)

    def get_product(self, item_id: str) -> ProductInfo | None:
        product = PRODUCTS.get(item_id)
        if product is None:
            return None
        return ProductInfo(item_id=item_id, title=product[0], image_url=None)


class DemoCarrier:
    """Cargo, in memory. Any shipment number exists; numbers ending in 0000 are cancelled."""

    def __init__(self) -> None:
        self._numbers = itertools.count(52_000_001)
        self._lock = threading.Lock()

    def create_shipment(self, request: ShipmentRequest) -> CreatedShipment:
        with self._lock:
            number = str(next(self._numbers))
        return CreatedShipment(
            external_id=number, status_code=1, status_text="נקלט", line_text="קו הדגמה"
        )

    def get_status(self, external_id: str, customer_code: int) -> ShipmentStatus:
        if not external_id.isdigit():
            raise ProviderError("shipment_number_invalid", sent=False)
        if external_id.endswith("0000"):
            return ShipmentStatus(code=8, text="בוטל")
        return ShipmentStatus(code=1, text="נקלט")

    def label_url(self, external_id: str) -> str:
        if not external_id.isdigit():
            raise ProviderError("shipment_number_invalid", sent=False)
        return f"https://api-v2.cargo.co.il/labels/demo/shipment_{external_id}_demo.pdf"


class DemoPrinter:
    """teva-printer, in memory. Every label prints at once."""

    def __init__(self) -> None:
        self._jobs = itertools.count(1)
        self._lock = threading.Lock()
        self.printed: list[str] = []

    def print_label(
        self,
        label_url: str,
        *,
        printer_id: str,
        direct: bool,
        credentials: PrintCredentials,
    ) -> PrintReceipt:
        if printer_id != DEMO_PRINTER_ID:
            raise ProviderError("printer_not_configured", sent=False)
        name = urlsplit(label_url).path.rsplit("/", 1)[-1]
        shipment = name.split("_")[1] if name.startswith("shipment_") else None
        with self._lock:
            job = next(self._jobs)
            self.printed.append(label_url)
        return PrintReceipt(
            job_id=f"demo-{job}", shipment=shipment, warning=None, job_state="completed"
        )

    def printers(self, credentials: PrintCredentials) -> list[dict[str, Any]]:
        return [{"id": DEMO_PRINTER_ID, "label": "מדפסת מדבקות (הדגמה)"}]
