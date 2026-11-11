"""Real clock, real sockets: a controllable local HTTP server behind HttpProvider.

Verifies timeout/cancellation cleanup and the absence of later dispatch. A closed client
connection does NOT prove the remote side stopped working; these tests only assert what
the local server can observe (arrivals and disconnects).
"""

import asyncio
import json
import time

import pytest

from gateway.config import Policy
from gateway.factory import build_app, build_gateway
from gateway.http_provider import HttpProvider
from gateway.simclient import asgi_post, request_body


class ControlledServer:
    """Per-path scripted behaviour: 'hang' (until the client disconnects), 'ok', '503', 'drop'."""

    def __init__(self, modes: dict[str, list[str]]):
        self.modes = modes
        self.arrivals: list[tuple[str, float, str | None]] = []   # (path, t, correlation id)
        self.disconnects: list[tuple[str, float]] = []
        self.port = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def count(self, path: str) -> int:
        return sum(1 for p, _, _ in self.arrivals if p == path)

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {k.lower(): v for k, _, v in (l.partition(": ") for l in lines[1:] if l)}
            await reader.readexactly(int(headers.get("content-length", "0")))
            n = self.count(path)
            self.arrivals.append((path, time.monotonic(), headers.get("x-correlation-id")))
            mode = self.modes[path][min(n, len(self.modes[path]) - 1)]
            if mode == "drop":                       # accept, then hang up without a response
                return
            if mode == "hang":
                try:
                    await reader.read(1)             # b"" once the client closes
                finally:
                    self.disconnects.append((path, time.monotonic()))
                return
            status, body = (200, {"output": "real-ok"}) if mode == "ok" else (503, {"error": "x"})
            payload = json.dumps(body).encode()
            writer.write(
                f"HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n"
                f"content-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


async def _rig(server, policy, sleep=None):
    primary = HttpProvider("primary", "real-primary-1", server.url("/primary"))
    fallback = HttpProvider("fallback", "real-fallback-1", server.url("/fallback"))
    events: list[dict] = []
    kwargs = {"sleep": sleep} if sleep else {}
    gw = build_gateway(primary, fallback, policy=policy, sink=events.append, **kwargs)
    return build_app(gw), primary, fallback, events


async def _close(*providers):
    for p in providers:
        await p.aclose()


def test_timeout_cancels_the_connection_then_retry_and_fallback_proceed():
    async def go():
        server = ControlledServer({"/primary": ["hang"], "/fallback": ["ok"]})
        await server.start()
        policy = Policy(total_deadline_s=2.0, per_call_timeout_s=0.2, retry_wait_s=0.05)
        app, p, f, events = await _rig(server, policy)
        try:
            t = time.monotonic()
            resp = await asgi_post(app)
            took = time.monotonic() - t
            await asyncio.sleep(0.2)                  # let disconnects be observed
            return resp, took, server, events
        finally:
            await _close(p, f)
            await server.stop()

    resp, took, server, events = asyncio.run(go())
    body = resp.json
    assert resp.status == 200 and body["provider"] == "fallback"
    assert body["execution_mode"] == "real" and body["attempts"] == 3
    assert (server.count("/primary"), server.count("/fallback")) == (2, 1)
    # Cleanup: each timed-out primary connection was closed from our side.
    assert len(server.disconnects) == 2
    assert 0.4 <= took < 1.0
    ends = [e for e in events if e["event"] == "attempt.end" and e["reason"] == "timeout"]
    assert len(ends) == 2 and all(e["remote_outcome"] == "uncertain" for e in ends)
    assert {c for _, _, c in server.arrivals} == {body["request_id"]}   # correlation only


def test_no_dispatch_after_the_deadline_when_the_retry_wait_stalls():
    async def go():
        server = ControlledServer({"/primary": ["hang"], "/fallback": ["ok"]})
        await server.start()

        async def stalled(_s):                          # injected sleeper: wakes after the deadline
            await asyncio.sleep(0.6)

        policy = Policy(total_deadline_s=0.5, per_call_timeout_s=0.2, retry_wait_s=0.05)
        app, p, f, events = await _rig(server, policy, sleep=stalled)
        try:
            resp = await asgi_post(app)
            await asyncio.sleep(0.4)                  # any stray late dispatch would show up now
            return resp, server
        finally:
            await _close(p, f)
            await server.stop()

    resp, server = asyncio.run(go())
    assert resp.status == 504 and resp.json["error"]["code"] == "deadline_exceeded"
    assert resp.json["attempts"] == 1
    assert (server.count("/primary"), server.count("/fallback")) == (1, 0)
    assert len(server.disconnects) == 1                # the one timed-out call was cleaned up


def test_client_disconnect_cancels_the_inflight_call_and_nothing_follows():
    async def go():
        server = ControlledServer({"/primary": ["hang"], "/fallback": ["ok"]})
        await server.start()
        app, p, f, events = await _rig(server, Policy())
        try:
            resp = await asgi_post(app, disconnect_at=0.15)
            await asyncio.sleep(0.9)                  # > 500 ms timeout + 100 ms retry wait
            return resp, server, events
        finally:
            await _close(p, f)
            await server.stop()

    resp, server, events = asyncio.run(go())
    assert resp.status is None                         # nothing written to a gone client
    assert (server.count("/primary"), server.count("/fallback")) == (1, 0)
    assert len(server.disconnects) == 1                # local cancellation closed the connection
    cancelled = [e for e in events if e["event"] == "attempt.end" and e["outcome"] == "cancelled"]
    assert len(cancelled) == 1 and cancelled[0]["remote_outcome"] == "uncertain"
    assert not [e for e in events if e["event"] == "request.finished"]


def test_dropped_connection_is_a_retryable_connection_failure():
    async def go():
        server = ControlledServer({"/primary": ["drop"], "/fallback": ["drop"]})
        await server.start()
        app, p, f, events = await _rig(server, Policy())
        try:
            return await asgi_post(app), events, server
        finally:
            await _close(p, f)
            await server.stop()

    resp, events, server = asyncio.run(go())
    assert resp.status == 503 and resp.json["attempts"] == 3
    assert {e["reason"] for e in events if e["event"] == "attempt.end"} == {"connection_failure"}
    assert (server.count("/primary"), server.count("/fallback")) == (2, 1)
