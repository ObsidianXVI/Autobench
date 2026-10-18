"""In-process event bus. Services publish changes (from any thread); WebSocket clients subscribe.

Message shape: {"type": "annotation.created" | "annotation.updated" | "annotation.deleted" |
"view.changed" | "analysis.created" | "note.saved" | "watchlist.updated", "data": {...}, "ts": epoch}"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any


class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.history: list[dict] = []  # small ring buffer, handy for tests and debugging

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def _fanout(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block publishers

    def publish(self, type_: str, data: Any) -> None:
        msg = {"type": type_, "data": data, "ts": time.time()}
        self.history.append(msg)
        del self.history[:-200]
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(msg)
        else:
            loop.call_soon_threadsafe(self._fanout, msg)


bus = EventBus()


def enable_http_relay(base_url: str) -> None:
    """In a separate MCP process (stdio mode) there are no WebSocket clients; forward every event
    to the running web server, which re-broadcasts it. Best effort: failures are ignored."""
    import httpx

    client = httpx.Client(timeout=2)
    url = base_url.rstrip("/") + "/api/events/relay"
    original = bus.publish

    def send(msg: dict) -> None:
        try:
            client.post(url, json=msg)
        except httpx.HTTPError:
            pass

    def publish(type_: str, data: Any) -> None:
        original(type_, data)
        threading.Thread(target=send, args=({"type": type_, "data": data},), daemon=True).start()

    bus.publish = publish  # type: ignore[method-assign]
