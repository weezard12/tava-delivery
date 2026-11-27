"""teva-printer, the workspace's print service, for Cargo labels.

In the workspace the employee's own session is passed on with each print (cookie, origin and
CSRF token, as the browser sent them), so teva-printer checks it with the auth service and
records who printed. A standalone teva-printer takes its PRINTER_TOKEN instead. The label link
travels in the request body, never in a URL that could be logged.
"""

from typing import Any
from urllib.parse import quote

import httpx

from ..domain.errors import ProviderError
from ..domain.ports import PrintCredentials, PrintReceipt

TIMEOUT = httpx.Timeout(45.0, connect=5.0)


class PrinterClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self, credentials: PrintCredentials) -> dict[str, str]:
        headers: dict[str, str] = {}
        if credentials.session and credentials.cookie_name:
            headers["Cookie"] = f"{credentials.cookie_name}={credentials.session}"
        if credentials.origin:
            headers["Origin"] = credentials.origin
        if credentials.csrf_token:
            headers["X-CSRF-Token"] = credentials.csrf_token
        return headers

    def print_label(
        self,
        label_url: str,
        *,
        printer_id: str,
        direct: bool,
        credentials: PrintCredentials,
    ) -> PrintReceipt:
        path = f"/api/v1/printers/{quote(printer_id, safe='')}/cargo-labels"
        try:
            response = self._client.post(
                path,
                json={"url": label_url, "copies": 1, "direct": direct},
                headers=self._headers(credentials),
            )
        except httpx.ConnectError, httpx.ConnectTimeout:
            raise ProviderError("printer_unavailable", retryable=True, sent=False) from None
        except httpx.HTTPError:
            # It may have printed: the employee checks the printer before printing again.
            raise ProviderError("print_outcome_unknown", sent=True) from None
        status = response.status_code
        if status != 202:
            # teva-printer answers 4xx, 502 and 503 before it hands anything to CUPS.
            code = _failure(status, _detail(response))
            raise ProviderError(code, retryable=status == 503, sent=code == "print_outcome_unknown")
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("print_outcome_unknown", sent=True) from None
        if not isinstance(data, dict) or not data.get("job_id"):
            raise ProviderError("print_outcome_unknown", sent=True)
        job_state = data.get("job_state")
        if job_state in ("aborted", "canceled"):
            raise ProviderError("print_failed", sent=True)
        return PrintReceipt(
            job_id=str(data["job_id"])[:200],
            shipment=str(data["shipment"]) if data.get("shipment") else None,
            warning=str(data["printer_warning"])[:60] if data.get("printer_warning") else None,
            job_state=str(job_state) if job_state else None,
        )

    def printers(self, credentials: PrintCredentials) -> list[dict[str, Any]]:
        """The printers teva-printer offers, for the settings screen."""
        try:
            response = self._client.get("/api/v1/printers", headers=self._headers(credentials))
        except httpx.HTTPError:
            raise ProviderError("printer_unavailable", sent=False) from None
        if response.status_code != 200:
            raise ProviderError("printer_unavailable", sent=False)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("printer_unavailable", sent=False) from None
        printers = data.get("printers") if isinstance(data, dict) else None
        return [
            {"id": str(item.get("id")), "label": str(item.get("label") or item.get("id"))}
            for item in printers or []
            if isinstance(item, dict) and item.get("id")
        ]


def _detail(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    detail = data.get("detail") if isinstance(data, dict) else None
    return detail if isinstance(detail, str) else None


# teva-printer's codes for a label it could not get from Cargo.
LABEL_CODES = frozenset(
    {"cargo_label_not_found", "cargo_link_invalid", "cargo_unavailable", "pdf_too_large"}
)


def _failure(status: int, detail: str | None) -> str:
    """What a refused print means here. Only a 5xx other than 502 and 503 may have printed."""
    if detail in LABEL_CODES:
        return "label_unavailable"
    if status == 401:
        return "invalid_session" if detail == "invalid_session" else "printer_not_configured"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "printer_not_configured"
    if status in (409, 413, 422):
        return "print_failed"
    if status == 502:
        return "label_unavailable"
    if status == 503:
        return "printer_unavailable"
    return "print_outcome_unknown" if status >= 500 else "print_failed"
