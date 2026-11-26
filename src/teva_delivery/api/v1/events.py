"""Live updates: a WebSocket that tells open screens what changed, so they refetch.

Events carry IDs and versions, never order details. The screen still polls slowly, so a
dropped connection only delays an update.
"""

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...domain.errors import ServiceError
from ..dependencies import authenticate, get_services

router = APIRouter()

PING_SECONDS = 25


async def _forward(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), PING_SECONDS)
        except TimeoutError:
            event = {"type": "ping"}
        await websocket.send_json(event)


async def _listen(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    try:
        await authenticate(websocket, "GET")
    except ServiceError as error:
        await websocket.close(code={401: 4401, 403: 4403}.get(error.status, 1013))
        return
    await websocket.accept()
    bus = get_services(websocket).context.events
    with bus.subscribe() as queue:
        tasks = {
            asyncio.create_task(_forward(websocket, queue)),
            asyncio.create_task(_listen(websocket)),
        }
        await websocket.send_json({"type": "hello"})
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in (*done, *pending):
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
