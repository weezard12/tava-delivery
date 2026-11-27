"""The events socket through a real Uvicorn server, as the image runs it.

The TestClient speaks WebSocket without Uvicorn, so it cannot see that Uvicorn has no
WebSocket implementation installed; this test can.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect

from teva_delivery.app import create_app

from ..support import make_settings


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    app = create_app(make_settings(tmp_path), start_worker=False)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("Uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        thread.join(timeout=10)


def test_the_events_socket_answers_through_uvicorn(server: str) -> None:
    with connect(f"ws://{server}/api/v1/events", open_timeout=5) as websocket:
        assert json.loads(websocket.recv(timeout=5)) == {"type": "hello"}
