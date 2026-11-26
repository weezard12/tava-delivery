"""Cargo's API v2, the one Cargo's own e-commerce plugins use.

Endpoints and fields follow Cargo's published WooCommerce plugin ("Cargo Shipping Location
for WooCommerce", includes/CargoApi/CargoAPIV2.php): ``shipments/create``,
``shipments/get-status`` and ``shipments/print-label`` under ``https://api-v2.cargo.co.il/api/``
with a bearer API token. Answers are ``{"errors": ..., "data": ..., "messages": ...}``.

Creating a shipment is not idempotent at Cargo and cannot be looked up by order, so a create
that got no answer is reported as possibly sent, and the service reconciles it with a person.
"""

import logging
from decimal import Decimal
from typing import Any

import httpx

from ..domain.errors import ProviderError
from ..domain.ports import CreatedShipment, PostalAddress, ShipmentRequest, ShipmentStatus

logger = logging.getLogger("teva_delivery.cargo")

BASE_URL = "https://api-v2.cargo.co.il/api/"
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Cargo's carrier IDs: 0 is a BOX pickup point, 1 the express courier.
CARRIER_BOX = 0
CARRIER_EXPRESS = 1
SHIPPING_TYPE_DELIVERY = 1


def _address(address: PostalAddress) -> dict[str, str]:
    return {
        "name": address.name,
        "company": address.company or address.name,
        "street1": address.street,
        "street2": "",
        "city": address.city,
        "country": "ישראל",
        "phone": address.phone,
        "email": address.email,
        "floor": address.floor,
        "appartment": address.apartment,  # Cargo's spelling
    }


def shipment_payload(request: ShipmentRequest, website: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "shipping_type": SHIPPING_TYPE_DELIVERY,
        "number_of_parcels": request.parcels,
        "double_delivery": 1,
        "total_value": str(request.total_value.quantize(Decimal("0.01"))),
        "transaction_id": request.reference,
        "cash_on_delivery": 0,
        "cod_type": 0,
        "carrier_id": CARRIER_BOX if request.box else CARRIER_EXPRESS,
        "order_id": request.reference,
        "notes": request.notes,
        "website": website,
        "platform": "Teva Warehouse",
        "customer_code": request.customer_code,
        "to_address": _address(request.recipient),
        "from_address": _address(request.sender),
    }
    if request.box and request.box_point_id:
        payload["box_point_id"] = request.box_point_id
    return payload


class CargoClient:
    def __init__(
        self,
        token: str,
        *,
        website: str,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.website = website
        self._client = httpx.Client(
            base_url=base_url,
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict[str, Any], *, write: bool = False) -> Any:
        try:
            response = self._client.post(path, json=body)
        except httpx.ConnectError, httpx.ConnectTimeout:
            raise ProviderError("cargo_unavailable", retryable=True, sent=False) from None
        except httpx.HTTPError:
            raise ProviderError("cargo_unavailable", retryable=True, sent=write) from None
        if response.status_code in (401, 403):
            raise ProviderError("cargo_not_configured", sent=False)
        if response.status_code == 404:
            raise ProviderError("shipment_not_found", sent=False)
        if response.status_code in (400, 422):
            raise ProviderError("cargo_rejected", sent=False)
        if response.status_code >= 500:
            raise ProviderError("cargo_unavailable", retryable=True, sent=write)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("cargo_unexpected_response", sent=write) from None
        if not isinstance(data, dict):
            raise ProviderError("cargo_unexpected_response", sent=write)
        if data.get("errors"):
            # Cargo answered and refused: nothing was created.
            raise ProviderError("cargo_rejected", sent=False)
        return data.get("data")

    def create_shipment(self, request: ShipmentRequest) -> CreatedShipment:
        data = self._post("shipments/create", shipment_payload(request, self.website), write=True)
        if not isinstance(data, dict) or not str(data.get("shipment_id") or "").isdigit():
            # Cargo said it worked but gave no number: it may exist, a person checks.
            raise ProviderError("cargo_unexpected_response", sent=True)
        return CreatedShipment(
            external_id=str(data["shipment_id"]),
            status_code=None,
            status_text=None,
            line_text=str(data["line_text"])[:200] if data.get("line_text") else None,
        )

    def get_status(self, external_id: str, customer_code: int) -> ShipmentStatus:
        if not external_id.isdigit():
            raise ProviderError("shipment_number_invalid", sent=False)
        data = self._post(
            "shipments/get-status",
            {"shipment_id": int(external_id), "customer_code": customer_code},
        )
        if not isinstance(data, dict):
            raise ProviderError("shipment_not_found", sent=False)
        code = data.get("status_code")
        return ShipmentStatus(
            code=int(code) if isinstance(code, int | str) and str(code).isdigit() else None,
            text=str(data["status_text"])[:200] if data.get("status_text") else None,
        )

    def label_url(self, external_id: str) -> str:
        if not external_id.isdigit():
            raise ProviderError("shipment_number_invalid", sent=False)
        data = self._post("shipments/print-label", {"shipment_ids": external_id})
        if not isinstance(data, str) or not data.startswith("https://"):
            raise ProviderError("label_unavailable", sent=False)
        return data
