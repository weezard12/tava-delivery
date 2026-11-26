"""The Konimbo client against Konimbo's documented API, served by a mock transport."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from teva_delivery.domain.enums import LineKind
from teva_delivery.domain.errors import ProviderError
from teva_delivery.providers.konimbo import KonimboClient, RequestBudget, next_page, parse_order

TOKEN = "konimbo-test-token"
SINCE = datetime(2026, 11, 1, tzinfo=UTC)


def order(number: int = 100, **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": number,
        "created_at": "2026-11-25T09:30:00+02:00",
        "status_option_title": "הזמנה חדשה",
        "payment_status": "שולם",
        "name": "דנה כהן",
        "phone": "050-1234567",
        "email": "dana@example.com",
        "address": "הרצל 12, תל אביב",
        "note": "לעטוף כמתנה",
        "total_price": "118.00",
        "credit_card_details": {"number": "never read"},
        "items": [
            {"id": 7, "title": "סבון זית", "code": "SOAP", "quantity": 2, "price": "59"},
            {"id": 7, "title": "סבון זית", "code": "SOAP", "quantity": 1, "price": "0"},
        ],
        "upgrades": [{"item_id": 7, "title": "אריזת מתנה", "topic_title": "תוספות", "quantity": 1}],
        "shipping": {"id": 3, "title": "שליח עד הבית", "price": "30"},
        "statuses": [
            {
                "id": 1,
                "status_option_title": "הזמנה חדשה",
                "username": "אתר",
                "comment": "",
                "created_at": "2026-11-25T09:30:00+02:00",
            }
        ],
    }
    data.update(fields)
    return data


def client(handler: Any, budget: int = 50) -> KonimboClient:
    return KonimboClient(TOKEN, budget=budget, transport=httpx.MockTransport(handler))


def test_parse_order_keeps_what_the_warehouse_needs() -> None:
    parsed = parse_order(order())
    assert parsed.id == "100"
    assert parsed.created_at == datetime(2026, 11, 25, 7, 30, tzinfo=UTC)
    assert [line.key for line in parsed.lines] == [
        "item:7",
        "item:7#2",
        "upgrade:7:תוספות:אריזת מתנה",
    ]
    assert parsed.lines[2].kind is LineKind.UPGRADE
    assert parsed.shipping_title == "שליח עד הבית"
    assert parsed.statuses[0].title == "הזמנה חדשה"
    assert "never read" not in repr(parsed)


def test_parse_order_refuses_an_order_without_id() -> None:
    with pytest.raises(ProviderError):
        parse_order({"created_at": "2026-11-25T09:30:00Z"})


def test_list_orders_follows_the_pagination_header() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json=[order(1)],
                headers={
                    "X-Pagination-Links": '<https://api.konimbo.co.il/v1/orders?page=2>; rel="next"'
                },
            )
        return httpx.Response(200, json=[order(2)])

    orders = list(client(handler).list_orders("הזמנה חדשה", SINCE))
    assert [o.id for o in orders] == ["1", "2"]
    params = seen[0].params
    assert params["token"] == TOKEN
    assert params["status_option_title"] == "הזמנה חדשה"
    assert params["created_at_min"] == "2026-11-01T00:00:00Z"
    assert "credit_card_details" not in params["attributes"]


def test_list_orders_stops_at_the_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[order(1), order(2), order(3)])

    assert len(list(client(handler).list_orders(None, SINCE, limit=2))) == 2


def test_append_status_sends_the_documented_body() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/v1/orders/100"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=order(status_option_title="ממתין לאיסוף"))

    updated = client(handler).append_status(
        "100", title="ממתין לאיסוף", comment="נארז", username="מחסן"
    )
    assert updated.status_title == "ממתין לאיסוף"
    assert bodies == [
        {
            "token": TOKEN,
            "order": {
                "statuses": [
                    {"status_option_title": "ממתין לאיסוף", "username": "מחסן", "comment": "נארז"}
                ]
            },
        }
    ]


@pytest.mark.parametrize(
    ("status", "headers", "code", "retryable"),
    [
        (401, {}, "konimbo_unauthorized", False),
        (
            401,
            {"X-Rate-Limit-Current": "100", "X-Rate-Limit-Maximum": "100"},
            "konimbo_rate_limited",
            True,
        ),
        (404, {}, "konimbo_order_not_found", False),
        (422, {}, "konimbo_rejected", False),
        (429, {}, "konimbo_rate_limited", True),
        (503, {}, "konimbo_unavailable", True),
    ],
)
def test_errors_have_stable_codes(
    status: int, headers: dict[str, str], code: str, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers)

    with pytest.raises(ProviderError) as error:
        client(handler).get_order("100")
    assert (error.value.code, error.value.retryable) == (code, retryable)


def test_a_write_that_timed_out_may_have_been_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as error:
        client(handler).append_status("100", title="x", comment="", username="מחסן")
    assert error.value.sent is True


def test_a_refused_connection_sent_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as error:
        client(handler).append_status("100", title="x", comment="", username="מחסן")
    assert error.value.sent is False


def test_only_numeric_ids_reach_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request expected")

    with pytest.raises(ProviderError):
        client(handler).get_order("../items")


def test_product_images_come_from_the_first_https_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/items/8":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "id": 7,
                "title": "סבון",
                "images": [
                    {"url": "http://insecure/x.jpg", "position": 0},
                    {"url": "https://cdn.example/b.jpg", "position": 2},
                    {"url": "https://cdn.example/a.jpg", "position": 1},
                ],
            },
        )

    product = client(handler).get_product("7")
    assert product is not None and product.image_url == "https://cdn.example/a.jpg"
    assert client(handler).get_product("8") is None


def test_the_request_budget_holds_back_before_konimbo_blocks() -> None:
    now = [0.0]
    budget = RequestBudget(2, clock=lambda: now[0])
    budget.take()
    budget.take()
    with pytest.raises(ProviderError) as error:
        budget.take()
    assert error.value.code == "konimbo_rate_limited"
    now[0] = 601
    budget.take()
    assert budget.left == 1


def test_konimbo_near_its_limit_stops_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=order(), headers={"X-Rate-Limit-Current": "99", "X-Rate-Limit-Maximum": "100"}
        )

    konimbo = client(handler)
    konimbo.get_order("100")
    assert konimbo.requests_left == 0


def test_next_page() -> None:
    header = (
        '<https://api.konimbo.co.il/v1/orders?page=1>; rel="prev", '
        '<https://api.konimbo.co.il/v1/orders?page=3>; rel="next"'
    )
    assert next_page(header) == 3
    assert next_page(None) is None
