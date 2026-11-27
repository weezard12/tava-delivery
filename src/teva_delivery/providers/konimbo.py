"""Konimbo's order API (v1), as documented in github.com/konimbo/API-Documentation.

Only documented endpoints are used: ``GET /orders`` (filtered by status and creation time),
``GET /orders/{id}``, ``PUT /orders/{id}`` (append statuses) and ``GET /items/{id}``. Every
read names the fields it wants (``attributes``), so payment card details are never asked
for; any field that comes back unasked is dropped by the parser and never stored or logged.

Konimbo allows 100 requests per 10 minutes per token and answers 401 both for a bad token and
for a blocked one. The client keeps its own budget below that, so the warehouse never gets
the store's token blocked.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..domain.enums import LineKind
from ..domain.errors import ProviderError
from ..domain.ports import ProductInfo, StoreLine, StoreOrder, StoreStatus

logger = logging.getLogger("teva_delivery.konimbo")

BASE_URL = "https://api.konimbo.co.il/v1"
ORDER_FIELDS = (
    "id,payment_status,name,email,phone,address,note,total_price,status_option_title,"
    "created_at,items,upgrades,shipping,statuses"
)
ITEM_FIELDS = "id,title,images"
WINDOW_SECONDS = 600
MAX_PAGES = 50
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def _text(value: object, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _quantity(value: object) -> int:
    try:
        return max(int(float(str(value))), 0)
    except ValueError:
        return 0


def parse_order(data: dict[str, Any]) -> StoreOrder:
    """Keep the fields the warehouse needs. Unknown and payment fields are dropped here."""
    order_id = _text(data.get("id"), 40)
    created = _time(data.get("created_at"))
    if order_id is None or created is None:
        raise ProviderError("konimbo_unexpected_response", sent=False)
    lines: list[StoreLine] = []
    seen: dict[str, int] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = _text(item.get("id"), 40)
        base = f"item:{item_id}" if item_id else f"sku:{_text(item.get('code'), 120)}"
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        quantity = _quantity(item.get("quantity"))
        if quantity <= 0:
            continue
        lines.append(
            StoreLine(
                key=key,
                item_id=item_id,
                sku=_text(item.get("code"), 120),
                title=_text(item.get("title")) or "מוצר",
                quantity=quantity,
            )
        )
    for upgrade in data.get("upgrades") or []:
        if not isinstance(upgrade, dict):
            continue
        title = _text(upgrade.get("title")) or "שדרוג"
        topic = _text(upgrade.get("topic_title"))
        quantity = _quantity(upgrade.get("quantity") or 1)
        if quantity <= 0:
            continue
        key = f"upgrade:{upgrade.get('item_id')}:{topic}:{title}"[:120]
        lines.append(
            StoreLine(
                key=key,
                item_id=None,
                sku=_text(upgrade.get("inventory_code"), 120),
                title=f"{topic}: {title}" if topic else title,
                quantity=quantity,
                kind=LineKind.UPGRADE,
            )
        )
    statuses = tuple(
        StoreStatus(
            title=_text(status.get("status_option_title"), 200) or "",
            comment=_text(status.get("comment"), 2000),
            username=_text(status.get("username"), 200),
            created_at=_time(status.get("created_at")),
        )
        for status in data.get("statuses") or []
        if isinstance(status, dict)
    )
    shipping = data.get("shipping") if isinstance(data.get("shipping"), dict) else {}
    return StoreOrder(
        id=order_id,
        created_at=created,
        status_title=_text(data.get("status_option_title"), 200),
        payment_status=_text(data.get("payment_status"), 200),
        customer_name=_text(data.get("name"), 300),
        email=_text(data.get("email"), 320),
        phone=_text(data.get("phone"), 60),
        address=_text(data.get("address"), 600),
        note=_text(data.get("note"), 2000),
        total_price=_decimal(data.get("total_price")),
        shipping_id=_text(shipping.get("id"), 60) if shipping else None,
        shipping_title=_text(shipping.get("title"), 300) if shipping else None,
        lines=tuple(lines),
        statuses=statuses,
    )


def next_page(header: str | None) -> int | None:
    """The page number of rel="next" in an X-Pagination-Links header, if there is one."""
    if not header:
        return None
    for part in header.split(","):
        if 'rel="next"' not in part:
            continue
        url = part.split(";", 1)[0].strip().strip("<>")
        pages = parse_qs(urlsplit(url).query).get("page")
        if pages and pages[0].isdigit():
            return int(pages[0])
    return None


class RequestBudget:
    """Requests per sliding ten minutes, kept below Konimbo's own limit."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.clock = clock
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
            self._sent.popleft()

    @property
    def left(self) -> int:
        with self._lock:
            self._trim(self.clock())
            return max(self.limit - len(self._sent), 0)

    def take(self) -> None:
        with self._lock:
            now = self.clock()
            self._trim(now)
            if len(self._sent) >= self.limit:
                raise ProviderError("konimbo_rate_limited", retryable=True, sent=False)
            self._sent.append(now)

    def exhaust(self) -> None:
        """Konimbo says the window is used up: stop until it rolls over."""
        with self._lock:
            now = self.clock()
            self._trim(now)
            while len(self._sent) < self.limit:
                self._sent.append(now)


class KonimboClient:
    def __init__(
        self,
        token: str,
        *,
        budget: int,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._budget = RequestBudget(budget, clock)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def requests_left(self) -> int:
        return self._budget.left

    def close(self) -> None:
        self._client.close()

    # Requests.

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self._budget.take()
        query = {"token": self._token, **(params or {})}
        write = method != "GET"
        try:
            if body is not None:
                response = self._client.request(
                    method, path, params=query, json={"token": self._token, **body}
                )
            else:
                response = self._client.request(method, path, params=query)
        except httpx.ConnectError, httpx.ConnectTimeout:
            raise ProviderError("konimbo_unavailable", retryable=True, sent=False) from None
        except httpx.HTTPError:
            # The request may have arrived: a write is uncertain until read back.
            raise ProviderError("konimbo_unavailable", retryable=True, sent=write) from None
        self._observe_limit(response)
        if response.status_code == 401:
            if self._blocked(response):
                self._budget.exhaust()
                raise ProviderError("konimbo_rate_limited", retryable=True, sent=False)
            raise ProviderError("konimbo_unauthorized", sent=False)
        if response.status_code == 404:
            raise ProviderError("konimbo_order_not_found", sent=False)
        if response.status_code in (400, 422):
            raise ProviderError("konimbo_rejected", sent=False)
        if response.status_code == 429:
            self._budget.exhaust()
            raise ProviderError("konimbo_rate_limited", retryable=True, sent=False)
        if response.status_code >= 500:
            raise ProviderError("konimbo_unavailable", retryable=True, sent=write)
        if response.status_code != 200:
            raise ProviderError("konimbo_unexpected_response", sent=write)
        return response

    def _observe_limit(self, response: httpx.Response) -> None:
        current = response.headers.get("x-rate-limit-current")
        maximum = response.headers.get("x-rate-limit-maximum")
        if (
            current
            and maximum
            and current.isdigit()
            and maximum.isdigit()
            and int(current) >= int(maximum) - 2
        ):
            logger.warning("Konimbo's request window is almost used up")
            self._budget.exhaust()

    def _blocked(self, response: httpx.Response) -> bool:
        current = response.headers.get("x-rate-limit-current")
        maximum = response.headers.get("x-rate-limit-maximum")
        return bool(
            current
            and maximum
            and current.isdigit()
            and maximum.isdigit()
            and int(current) >= int(maximum)
        )

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise ProviderError("konimbo_unexpected_response", sent=False) from None

    # The store port.

    def list_orders(
        self, status: str | None, created_after: datetime, *, limit: int | None = None
    ) -> Iterator[StoreOrder]:
        params = {
            "attributes": ORDER_FIELDS,
            "created_at_min": created_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort_by": "id",
        }
        if status:
            params["status_option_title"] = status
        page: int | None = 1
        seen_pages: set[int] = set()
        count = 0
        while page is not None and page not in seen_pages and len(seen_pages) < MAX_PAGES:
            seen_pages.add(page)
            response = self._request("GET", "/orders", params={**params, "page": str(page)})
            data = self._json(response)
            if not isinstance(data, list):
                raise ProviderError("konimbo_unexpected_response", sent=False)
            for entry in data:
                if isinstance(entry, dict):
                    yield parse_order(entry)
                    count += 1
                    if limit is not None and count >= limit:
                        return
            page = next_page(response.headers.get("x-pagination-links")) if data else None

    def get_order(self, order_id: str) -> StoreOrder:
        response = self._request(
            "GET", f"/orders/{_path(order_id)}", params={"attributes": ORDER_FIELDS}
        )
        data = self._json(response)
        if not isinstance(data, dict):
            raise ProviderError("konimbo_unexpected_response", sent=False)
        return parse_order(data)

    def append_status(
        self, order_id: str, *, title: str, comment: str, username: str
    ) -> StoreOrder:
        status: dict[str, str] = {"status_option_title": title, "username": username}
        if comment:
            status["comment"] = comment
        response = self._request(
            "PUT",
            f"/orders/{_path(order_id)}",
            params={"attributes": ORDER_FIELDS},
            body={"order": {"statuses": [status]}},
        )
        data = self._json(response)
        if not isinstance(data, dict):
            raise ProviderError("konimbo_unexpected_response", sent=True)
        try:
            return parse_order(data)
        except ProviderError:
            raise ProviderError("konimbo_unexpected_response", sent=True) from None

    def get_product(self, item_id: str) -> ProductInfo | None:
        try:
            response = self._request(
                "GET", f"/items/{_path(item_id)}", params={"attributes": ITEM_FIELDS}
            )
        except ProviderError as error:
            if error.code == "konimbo_order_not_found":
                return None
            raise
        data = self._json(response)
        if not isinstance(data, dict):
            return None
        images = [image for image in data.get("images") or [] if isinstance(image, dict)]
        images.sort(key=lambda image: _quantity(image.get("position") or 0))
        url = next(
            (
                str(image["url"])
                for image in images
                if isinstance(image.get("url"), str) and image["url"].startswith("https://")
            ),
            None,
        )
        return ProductInfo(item_id=item_id, title=_text(data.get("title")), image_url=url)


def _path(value: str) -> str:
    """Order and item IDs are digits; anything else never reaches the URL."""
    if not value.isdigit() or len(value) > 20:
        raise ProviderError("konimbo_order_not_found", sent=False)
    return value
