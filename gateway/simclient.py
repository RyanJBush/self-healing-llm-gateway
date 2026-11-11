"""A fake ASGI client: drives the app in-process on any event loop (simulated or real)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

PATH = "/v1/generations"
DEFAULT_RID = "123e4567-e89b-42d3-a456-426614174000"


@dataclass
class Response:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes = b""
    sent: list[dict] = field(default_factory=list)

    @property
    def json(self) -> dict:
        return json.loads(self.raw)


def request_body(**overrides: Any) -> bytes:
    data = {"request_id": DEFAULT_RID, "input": "hello", "route": "standard"}
    data.update(overrides)
    return json.dumps(data).encode()


async def asgi_post(
    app: Any,
    body: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    path: str = PATH,
    body_delay: float = 0.0,
    disconnect_at: float | None = None,
) -> Response:
    """Drive the ASGI app once. Delays are seconds after the call starts, on the loop clock."""
    if body is None:
        body = request_body()
    hdrs = {"content-type": "application/json"} if headers is None else headers
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    queue: list[tuple[float, dict]] = [
        (body_delay, {"type": "http.request", "body": body, "more_body": False})
    ]
    if disconnect_at is not None:
        queue.append((disconnect_at, {"type": "http.disconnect"}))
    queue.sort(key=lambda item: item[0])

    async def receive() -> dict:
        if not queue:
            await loop.create_future()  # nothing more will ever arrive
        at, msg = queue.pop(0)
        delay = t0 + at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return msg

    resp = Response()

    async def send(msg: dict) -> None:
        resp.sent.append(msg)
        if msg["type"] == "http.response.start":
            resp.status = msg["status"]
            resp.headers = {k.decode(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            resp.raw += msg.get("body", b"")

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
    }
    await app(scope, receive, send)
    return resp
