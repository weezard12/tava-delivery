"""Cargo's API v2 client and the teva-printer client, against mock transports."""

import dataclasses
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from teva_delivery.domain.errors import ProviderError
from teva_delivery.domain.ports import PostalAddress, PrintCredentials, ShipmentRequest
from teva_delivery.providers.cargo import CargoClient, shipment_payload
from teva_delivery.providers.printer import PrinterClient

REQUEST = ShipmentRequest(
    reference="100",
    customer_code=1234,
    box=False,
    recipient=PostalAddress(name="דנה", street="הרצל 12", city="תל אביב", phone="0501234567"),
    sender=PostalAddress(name="לוין טבע", street="התעשייה 1", city="כרכור", phone="046000000"),
    total_value=Decimal(118),
    notes="להשאיר ליד הדלת",
)
LABEL = "https://api-v2.cargo.co.il/labels/2026-11-26/shipment_5550001_x.pdf"
SESSION = PrintCredentials(
    cookie_name="__Host-teva_session",
    session="s" * 40,
    origin="https://teva.example",
    csrf_token="csrf-token",
)


def cargo(handler: Any) -> CargoClient:
    return CargoClient(
        "cargo-token", website="https://shop.example", transport=httpx.MockTransport(handler)
    )


def test_the_payload_follows_cargos_plugin() -> None:
    payload = shipment_payload(REQUEST, "https://shop.example")
    assert payload["carrier_id"] == 1
    assert payload["total_value"] == "118.00"
    assert payload["to_address"]["appartment"] == ""
    assert payload["order_id"] == payload["transaction_id"] == "100"
    box = shipment_payload(dataclasses.replace(REQUEST, box=True, box_point_id="77"), "x")
    assert (box["carrier_id"], box["box_point_id"]) == (0, "77")


def test_create_shipment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/shipments/create"
        assert request.headers["authorization"] == "Bearer cargo-token"
        assert json.loads(request.content)["customer_code"] == 1234
        return httpx.Response(200, json={"errors": None, "data": {"shipment_id": 5550001}})

    assert cargo(handler).create_shipment(REQUEST).external_id == "5550001"


def test_a_refusal_created_nothing_and_a_timeout_may_have() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": ["bad phone"], "data": None})

    with pytest.raises(ProviderError) as error:
        cargo(refused).create_shipment(REQUEST)
    assert (error.value.code, error.value.sent) == ("cargo_rejected", False)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as error:
        cargo(slow).create_shipment(REQUEST)
    assert error.value.sent is True

    def no_number(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": None, "data": {}})

    with pytest.raises(ProviderError) as error:
        cargo(no_number).create_shipment(REQUEST)
    assert (error.value.code, error.value.sent) == ("cargo_unexpected_response", True)


def test_status_and_label() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get-status"):
            return httpx.Response(200, json={"data": {"status_code": "3", "status_text": "נמסר"}})
        return httpx.Response(200, json={"data": LABEL})

    client = cargo(handler)
    status = client.get_status("5550001", 1234)
    assert (status.code, status.text) == (3, "נמסר")
    assert client.label_url("5550001") == LABEL
    with pytest.raises(ProviderError):
        client.label_url("55-1")


def test_a_label_that_is_not_a_link_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "http://not-secure/label.pdf"})

    with pytest.raises(ProviderError) as error:
        cargo(handler).label_url("5550001")
    assert error.value.code == "label_unavailable"


def printer(handler: Any, token: str = "") -> PrinterClient:
    return PrinterClient("http://printer:8000", token=token, transport=httpx.MockTransport(handler))


def test_a_print_passes_the_employee_session_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/api/v1/printers/label%2F1/cargo-labels"
        assert request.headers["cookie"] == f"__Host-teva_session={'s' * 40}"
        assert request.headers["origin"] == "https://teva.example"
        assert request.headers["x-csrf-token"] == "csrf-token"
        assert json.loads(request.content) == {"url": LABEL, "copies": 1, "direct": True}
        return httpx.Response(
            202, json={"job_id": "label-12", "job_state": "processing", "shipment": "5550001"}
        )

    receipt = printer(handler).print_label(
        LABEL, printer_id="label/1", direct=True, credentials=SESSION
    )
    assert (receipt.job_id, receipt.shipment, receipt.job_state) == (
        "label-12",
        "5550001",
        "processing",
    )


@pytest.mark.parametrize(
    ("status", "detail", "code", "sent"),
    [
        (401, "invalid_session", "invalid_session", False),
        (401, "invalid_token", "printer_not_configured", False),
        (403, "forbidden", "forbidden", False),
        (404, "printer_not_found", "printer_not_configured", False),
        (404, "cargo_label_not_found", "label_unavailable", False),
        (409, "printer_not_accepting", "print_failed", False),
        (422, "cargo_link_invalid", "label_unavailable", False),
        (502, "cargo_unavailable", "label_unavailable", False),
        (503, "cups_unavailable", "printer_unavailable", False),
        (500, "internal_error", "print_outcome_unknown", True),
    ],
)
def test_print_failures(status: int, detail: str, code: str, sent: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": detail})

    with pytest.raises(ProviderError) as error:
        printer(handler).print_label(LABEL, printer_id="p", direct=True, credentials=SESSION)
    assert (error.value.code, error.value.sent) == (code, sent)


def test_a_lost_answer_may_have_printed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as error:
        printer(handler).print_label(LABEL, printer_id="p", direct=True, credentials=SESSION)
    assert (error.value.code, error.value.sent) == ("print_outcome_unknown", True)


def test_an_aborted_job_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"job_id": "x", "job_state": "aborted"})

    with pytest.raises(ProviderError) as error:
        printer(handler).print_label(LABEL, printer_id="p", direct=True, credentials=SESSION)
    assert error.value.code == "print_failed"


def test_the_printer_list_and_the_standalone_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer printer-token"
        return httpx.Response(
            200, json={"printers": [{"id": "label", "label": "מדבקות"}, {"id": "a4"}, {"x": 1}]}
        )

    listed = printer(handler, token="printer-token").printers(PrintCredentials())
    assert listed == [{"id": "label", "label": "מדבקות"}, {"id": "a4", "label": "a4"}]
