"""Tells open packing screens that something changed, so they refetch at once.

Events carry IDs and versions only. Each WebSocket connection subscribes with its own queue;
services publish from worker threads after their transaction commits.
"""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

MAX_PENDING = 200


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = (
            set()
        )

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(MAX_PENDING)
        entry = (asyncio.get_running_loop(), queue)
        with self._lock:
            self._subscribers.add(entry)
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers.discard(entry)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, event)
            except RuntimeError:
                # The connection's loop has closed; it unsubscribes on its way out.
                continue

    def fulfillment_changed(self, fulfillment_id: int, version: int) -> None:
        self.publish({"type": "fulfillment", "id": fulfillment_id, "version": version})

    def queue_changed(self) -> None:
        self.publish({"type": "queue"})


def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    if queue.full():
        # A slow screen misses old events; it refetches on the next one anyway.
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(event)
