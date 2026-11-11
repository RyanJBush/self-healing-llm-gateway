"""Provider interface and deterministic stubs.

A provider returns what an HTTP client would: status, headers, parsed body. The
gateway (engine.py) classifies it. A provider raises ``ConnectionFailure`` for
transport-level failure and nothing else; any other exception is a defect and
becomes a 500.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class ConnectionFailure(Exception):
    """Transport failure: refused, reset, DNS, TLS."""


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass(frozen=True)
class CallContext:
    """Correlation data for one dispatched call. ``request_id`` correlates events only;
    it is NOT an idempotency key and gives no exactly-once guarantee."""

    request_id: str
    provider_attempt: int  # 1-based, per provider, within this request
    attempt: int  # 1-based, across providers, within this request


class Provider(Protocol):
    id: str
    model: str
    execution_mode: str  # "stub" or "real"

    async def call(self, text: str, ctx: CallContext) -> RawResponse: ...


# ---------------------------------------------------------------------------
# Stub scenario steps. Trusted config only; the client can never pick one.
# ---------------------------------------------------------------------------

_UNSET: Any = object()


@dataclass(frozen=True)
class Step:
    kind: str  # ok | http | connection_error | hang | malformed | crash
    delay_s: float = 0.0
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = _UNSET


def ok(delay_s: float = 0.0) -> Step:
    return Step("ok", delay_s)


def http_error(
    status: int, retry_after: str | None = None, delay_s: float = 0.0, body: Any = _UNSET
) -> Step:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return Step("http", delay_s, status, headers, body)


def connection_error(delay_s: float = 0.0) -> Step:
    return Step("connection_error", delay_s)


def hang() -> Step:
    return Step("hang")


def malformed(status: int = 200, body: Any = None, delay_s: float = 0.0) -> Step:
    return Step("malformed", delay_s, status, {}, body)


def crash(delay_s: float = 0.0) -> Step:
    return Step("crash", delay_s)


def step_from_dict(data: Mapping[str, Any]) -> Step:
    """Parse one step from the operator's scenario file."""
    kind = data["kind"]
    delay_s = float(data.get("delay_ms", 0)) / 1000.0
    if kind == "ok":
        return ok(delay_s)
    if kind == "http":
        return http_error(int(data["status"]), data.get("retry_after"), delay_s)
    if kind == "connection_error":
        return connection_error(delay_s)
    if kind == "hang":
        return hang()
    if kind == "malformed":
        return malformed(int(data.get("status", 200)), data.get("body"), delay_s)
    if kind == "crash":
        return crash(delay_s)
    raise ValueError(f"unknown scenario step kind: {kind!r}")


def stub_output(provider_id: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"stub:{provider_id}:{digest}"


async def play_step(step: Step, provider_id: str, text: str) -> RawResponse:
    """Execute one scripted step the way a real client would observe it."""
    loop = asyncio.get_running_loop()
    if step.kind == "hang":
        await loop.create_future()  # never resolves; cancel-safe
    if step.delay_s > 0:
        await asyncio.sleep(step.delay_s)

    if step.kind == "ok":
        return RawResponse(200, {}, {"output": stub_output(provider_id, text)})
    if step.kind == "http":
        body = {"error": "upstream detail"} if step.body is _UNSET else step.body
        return RawResponse(step.status, dict(step.headers), body)
    if step.kind == "connection_error":
        raise ConnectionFailure("connection refused")
    if step.kind == "malformed":
        return RawResponse(step.status, {}, step.body)
    if step.kind == "crash":
        raise RuntimeError("stub defect: secret-internal-detail")
    raise AssertionError(f"unhandled step kind {step.kind!r}")


class StubProvider:
    """Deterministic provider. The n-th call runs ``script[n]``; once the script
    is exhausted the last step repeats. An empty script means always ``ok``."""

    execution_mode = "stub"

    def __init__(self, provider_id: str, model: str, script: list[Step] | None = None):
        self.id = provider_id
        self.model = model
        self._script = list(script) if script else [ok()]
        self.call_count = 0
        self.call_times: list[float] = []  # loop time at each call start

    async def call(self, text: str, ctx: CallContext | None = None) -> RawResponse:
        loop = asyncio.get_running_loop()
        step = self._script[min(self.call_count, len(self._script) - 1)]
        self.call_count += 1
        self.call_times.append(loop.time())
        return await play_step(step, self.id, text)
