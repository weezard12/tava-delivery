"""The HTTP API and the live-update WebSocket, with the workspace auth service faked."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from teva_delivery.app import create_app, services_of
from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.ports import PrintCredentials, PrintReceipt, Transcript
from teva_delivery.providers.demo import DemoPrinter
from teva_delivery.services.container import Services

from ..support import make_settings, settle

ADMIN_TOKEN = "a" * 32
USER_TOKEN = "u" * 32
COOKIE = "__Host-teva_session"
ORIGIN = "https://teva.example"


def auth_service(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    accounts = {ADMIN_TOKEN: ("manager", "admin"), USER_TOKEN: ("dana", "user")}
    if body["token"] not in accounts:
        return httpx.Response(401)
    if body["method"] != "GET" and body["csrf_token"] != "csrf":
        return httpx.Response(403)
    account, role = accounts[body["token"]]
    return httpx.Response(200, json={"id": account, "role": role})


class RecordingPrinter(DemoPrinter):
    def __init__(self) -> None:
        super().__init__()
        self.credentials: list[PrintCredentials] = []

    def print_label(
        self, label_url: str, *, printer_id: str, direct: bool, credentials: PrintCredentials
    ) -> PrintReceipt:
        self.credentials.append(credentials)
        return super().print_label(
            label_url, printer_id=printer_id, direct=direct, credentials=credentials
        )


class FakeTranscriber:
    def transcribe(self, audio: bytes, content_type: str) -> Transcript:
        return Transcript(text="מה עוד חסר?", confidence=0.95)


@pytest.fixture
def client(tmp_path: Path, clock: ManualClock) -> Iterator[TestClient]:
    settings = make_settings(
        tmp_path, auth_disabled=False, auth_url="http://auth.internal", auth_service_key="k" * 40
    )
    app = create_app(
        settings, clock=clock, auth_transport=httpx.MockTransport(auth_service), start_worker=False
    )
    with TestClient(app) as test_client:
        services_of(app).context.providers.printer = RecordingPrinter()
        services_of(app).sync.run()
        yield test_client


def headers(token: str = USER_TOKEN) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={token}", "Origin": ORIGIN, "X-CSRF-Token": "csrf"}


def services(client: TestClient) -> Services:
    return services_of(client.app)  # type: ignore[arg-type]


def test_health_needs_no_sign_in(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/livez").json() == {"status": "ok"}


def test_every_api_call_needs_a_workspace_session(client: TestClient) -> None:
    response = client.get("/api/v1/info")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"
    assert response.json()["detail"]
    forged = client.post("/api/v1/work/next", headers={**headers(), "X-CSRF-Token": "wrong"})
    assert forged.status_code == 403


def test_settings_are_for_admins(client: TestClient) -> None:
    change = {"changes": {"status_urgent": "דחוף"}}
    assert client.patch("/api/v1/settings", json=change, headers=headers()).status_code == 403
    response = client.patch("/api/v1/settings", json=change, headers=headers(ADMIN_TOKEN))
    assert response.status_code == 200
    assert response.json()["config"]["status_urgent"] == "דחוף"
    unknown = client.patch(
        "/api/v1/settings", json={"changes": {"token": "x"}}, headers=headers(ADMIN_TOKEN)
    )
    assert unknown.json()["code"] == "settings_invalid"


def test_packing_over_http(client: TestClient, clock: ManualClock) -> None:
    info = client.get("/api/v1/info", headers=headers()).json()
    assert info["mode"] == "demo" and info["printer"]["ready"]
    work = client.post("/api/v1/work/next", headers=headers()).json()
    order = work["fulfillment"]
    path = f"/api/v1/fulfillments/{order['id']}/actions"
    for line in order["lines"]:
        result = client.post(
            path,
            json={"action": "confirm_product", "line_id": line["id"], "everything": True},
            headers=headers(),
        )
        assert result.status_code == 200 and result.json()["ok"]
    for note in order["notes"]:
        if note["requires_confirmation"]:
            client.post(
                path,
                json={"action": "confirm_order_note", "note_id": note["id"]},
                headers=headers(),
            )
    done = client.post(path, json={"action": "complete_order"}, headers=headers())
    assert done.json()["fulfillment"]["state"] == "packed"

    settle(services(client), clock)
    printed = client.post(
        path, json={"action": "print_shipping_label", "print_mode": "manual"}, headers=headers()
    )
    assert printed.status_code == 200, printed.json()
    printer = services(client).context.providers.printer
    assert isinstance(printer, RecordingPrinter)
    # teva-printer gets the employee's own session, so it records who printed.
    assert printer.credentials[0].session == USER_TOKEN
    assert printer.credentials[0].csrf_token == "csrf"

    again = client.post(
        path, json={"action": "print_shipping_label", "print_mode": "manual"}, headers=headers()
    )
    assert again.status_code == 409
    assert again.json()["code"] == "already_printed"


def test_errors_carry_a_code_and_a_hebrew_message(client: TestClient) -> None:
    missing = client.get("/api/v1/fulfillments/9999", headers=headers())
    assert missing.status_code == 404
    assert missing.json() == {"code": "fulfillment_not_found", "detail": "ההזמנה לא נמצאה."}
    invalid = client.post(
        "/api/v1/fulfillments/1/actions", json={"action": "launch_rocket"}, headers=headers()
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request_invalid"
    assert "launch_rocket" not in invalid.text
    assert client.get("/api/v1/nothing-here", headers=headers()).json()["code"] == "not_found"


def test_voice_text_and_audio(client: TestClient) -> None:
    work = client.post("/api/v1/work/next", headers=headers()).json()
    body = {
        "transcript": "מה עוד חסר?",
        "utterance_id": "utt-api-0001",
        "fulfillment_id": work["fulfillment"]["id"],
    }
    spoken = client.post("/api/v1/voice/text", json=body, headers=headers()).json()
    assert spoken["outcome"] == "executed"

    files = {"audio": ("clip.webm", b"\x1a\x45\xdf\xa3", "audio/webm")}
    form = {"utterance_id": "utt-api-0002", "fulfillment_id": str(work["fulfillment"]["id"])}
    unavailable = client.post("/api/v1/voice/audio", files=files, data=form, headers=headers())
    assert unavailable.json()["code"] == "stt_not_configured"

    services(client).context.providers.transcriber = FakeTranscriber()
    heard = client.post("/api/v1/voice/audio", files=files, data=form, headers=headers()).json()
    assert heard["transcript"] == "מה עוד חסר?"


def test_shipment_numbers_are_digits(client: TestClient) -> None:
    response = client.post(
        "/api/v1/fulfillments/1/shipment/link", json={"number": "abc123"}, headers=headers()
    )
    assert response.json()["code"] == "request_invalid"


def test_duplicates_aliases_and_sync(client: TestClient) -> None:
    reviews = client.get("/api/v1/duplicates", headers=headers()).json()
    assert reviews and reviews[0]["orders"][0]["masked_phone"].startswith("•")
    work = client.post("/api/v1/work/next", headers=headers()).json()
    line = work["fulfillment"]["lines"][0]
    alias = client.post(
        "/api/v1/aliases", json={"line_id": line["id"], "alias": "הבקבוק הקטן"}, headers=headers()
    )
    assert alias.status_code == 201
    listed = client.get("/api/v1/aliases", headers=headers()).json()
    assert [row["alias"] for row in listed] == ["הבקבוק הקטן"]
    assert (
        client.delete(f"/api/v1/aliases/{alias.json()['id']}", headers=headers()).status_code == 204
    )
    sync = client.get("/api/v1/sync", headers=headers()).json()
    assert sync["runs"][0]["kind"]
    assert client.post("/api/v1/sync/run", headers=headers()).status_code == 403
    assert client.post("/api/v1/sync/run", headers=headers(ADMIN_TOKEN)).status_code == 200
    printers = client.get("/api/v1/printers", headers=headers(ADMIN_TOKEN)).json()
    assert printers == [{"id": "demo-label", "label": "מדפסת מדבקות (הדגמה)"}]


def test_live_updates(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/events", headers=headers()) as socket:
        assert socket.receive_json() == {"type": "hello"}
        work = client.post("/api/v1/work/next", headers=headers()).json()
        events: list[dict[str, Any]] = [socket.receive_json(), socket.receive_json()]
        assert {"type": "fulfillment", "id": work["fulfillment"]["id"]}.items() <= next(
            event for event in events if event["type"] == "fulfillment"
        ).items()


def test_live_updates_need_a_session(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/v1/events") as socket,
    ):
        socket.receive_json()
    assert closed.value.code == 4401
