"""Pure ASGI app: POST /v1/generations. No framework, so no default validation payloads."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .config import DEADLINE_EPSILON_S
from .engine import Gateway, Outcome
from .envelope import MESSAGE_FOR_CODE, REASON_FOR_CODE, STATUS_FOR_CODE, error_body
from .safelog import describe
from .validation import MSG_TOO_LARGE, InvalidRequest, parse_request

PATH = "/v1/generations"
log = logging.getLogger("gateway.app")


class GatewayApp:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.policy = gateway.policy

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(receive, send)
        elif kind == "http":
            await self._guarded_http(scope, receive, send)
        else:  # websocket etc. are not served
            await send({"type": "websocket.close", "code": 1008})

    # -- lifespan ----------------------------------------------------------

    @staticmethod
    async def _lifespan(receive: Any, send: Any) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- http --------------------------------------------------------------

    async def _guarded_http(self, scope: dict, receive: Any, send: Any) -> None:
        """Last line of defence: an unexpected exception anywhere becomes a sanitized 500
        and a log line with the exception type and frames only (never its text), instead
        of the server's default traceback print."""
        started = False

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        begin = asyncio.get_running_loop().time()
        try:
            await self._http(scope, receive, tracking_send)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("unexpected handler exception %s", describe(exc))
            if not started:
                elapsed = round((asyncio.get_running_loop().time() - begin) * 1000)
                body = error_body(
                    None, "internal_error", MESSAGE_FOR_CODE["internal_error"], 0, elapsed
                )
                await self._send_json(send, STATUS_FOR_CODE["internal_error"], body)

    async def _http(self, scope: dict, receive: Any, send: Any) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()  # handler entry: the deadline starts here
        deadline = start + self.policy.total_deadline_s
        gw = self.gateway

        def ms() -> int:
            return round((loop.time() - start) * 1000)

        async def finish(outcome: Outcome, extra: dict | None = None) -> None:
            rid = outcome.body.get("request_id")
            gw.emit(
                "request.finished",
                rid,
                start,
                outcome="success" if outcome.code is None else outcome.code,
                reason=outcome.reason,
                status=outcome.status,
                attempts=outcome.attempts,
                fallback_used=outcome.body.get("fallback_used", False),
            )
            await self._send_json(send, outcome.status, outcome.body, extra)

        def simple_error(
            code: str,
            request_id: str | None = None,
            message: str | None = None,
        ) -> Outcome:
            body = error_body(request_id, code, message or MESSAGE_FOR_CODE[code], 0, ms())
            return Outcome(STATUS_FOR_CODE[code], body, code, 0, REASON_FOR_CODE[code])

        if scope["path"] != PATH:
            return await finish(simple_error("not_found"))
        if scope["method"] != "POST":
            return await finish(
                simple_error("method_not_allowed"), {"allow": "POST"}
            )

        headers = scope.get("headers", [])
        content_types = [v for k, v in headers if k.lower() == b"content-type"]
        content_type = content_types[0].decode("latin-1") if len(content_types) == 1 else None
        declared = [v for k, v in headers if k.lower() == b"content-length"]
        too_big_declared = False
        if declared:
            try:
                too_big_declared = int(declared[0]) > self.policy.max_body_bytes
            except ValueError:
                pass

        # Read the body inside the same deadline.
        if too_big_declared:
            status, raw = "too_large", b""
        else:
            status, raw = await self._read_body(receive, deadline)
        if status == "disconnected":
            gw.emit("request.cancelled", None, start, outcome="cancelled", reason="client_disconnected", phase="body")
            return
        if loop.time() >= deadline - DEADLINE_EPSILON_S or status == "deadline":
            return await finish(simple_error("deadline_exceeded"))
        if status == "too_large":
            return await finish(simple_error("invalid_request", None, MSG_TOO_LARGE))

        try:
            req = parse_request(content_type, raw)
        except InvalidRequest as exc:
            return await finish(simple_error("invalid_request", exc.request_id, exc.message))

        # Run the operation; cancel it (and make no further provider calls) if the
        # client disconnects.
        work = asyncio.create_task(gw.execute(req, start, deadline))
        watcher = asyncio.create_task(self._wait_disconnect(receive))
        try:
            await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                gw.emit(
                    "request.cancelled",
                    req.request_id,
                    start,
                    outcome="cancelled",
                    reason="client_disconnected",
                    phase="execute",
                )
                return
            outcome = work.result()
        finally:
            watcher.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(watcher, work, return_exceptions=True)
        await finish(outcome)

    @staticmethod
    async def _wait_disconnect(receive: Any) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    async def _read_body(self, receive: Any, deadline: float) -> tuple[str, bytes]:
        loop = asyncio.get_running_loop()
        if loop.time() >= deadline - DEADLINE_EPSILON_S:
            return "deadline", b""
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    msg = await receive()
                    if msg["type"] == "http.disconnect":
                        return "disconnected", b""
                    if msg["type"] != "http.request":
                        continue
                    chunk = msg.get("body", b"")
                    size += len(chunk)
                    if size > self.policy.max_body_bytes:
                        return "too_large", b""
                    chunks.append(chunk)
                    if not msg.get("more_body", False):
                        return "ok", b"".join(chunks)
        except TimeoutError:
            return "deadline", b""

    @staticmethod
    async def _send_json(
        send: Any, status: int, body: dict, extra: dict | None = None
    ) -> None:
        payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("ascii")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ]
        for name, value in (extra or {}).items():
            headers.append((name.encode("ascii"), value.encode("ascii")))
        try:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": payload})
        except OSError:
            pass  # client already gone
