"""Test harness: a virtual-time event loop and a fake ASGI client.

On the virtual loop, ``asyncio.sleep`` and ``asyncio.timeout`` advance a fake clock
instead of waiting, so the real 2000 ms / 500 ms / 100 ms policy is tested exactly.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from gateway.config import BreakerConfig, Policy
from gateway.factory import build_app, build_gateway
from gateway.providers import StubProvider, Step

REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
PATH = "/v1/generations"


from gateway.simclock import SimulatedLoop as VirtualTimeLoop, run_simulated as run  # noqa: E402


from gateway.simclient import PATH, Response, asgi_post, request_body  # noqa: E402


@dataclass
class Rig:
    app: Any
    primary: StubProvider
    fallback: StubProvider
    events: list[dict]

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def of(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


def make_rig(
    primary: list[Step] | None = None,
    fallback: list[Step] | None = None,
    policy: Policy | None = None,
    breaker: BreakerConfig | None = None,
    wall_clock: Callable[[], float] | None = None,
    sleep: Callable | None = None,
) -> Rig:
    events: list[dict] = []
    p = StubProvider("primary", "stub-primary-1", primary)
    f = StubProvider("fallback", "stub-fallback-1", fallback)
    kwargs = {}
    if wall_clock:
        kwargs["wall_clock"] = wall_clock
    if sleep:
        kwargs["sleep"] = sleep
    gw = build_gateway(p, f, policy=policy, breaker=breaker, sink=events.append, **kwargs)
    return Rig(build_app(gw), p, f, events)


def body_for(**overrides: Any) -> bytes:
    return request_body(**overrides)


async def post(rig: Rig, body: bytes | None = None, **kwargs: Any) -> Response:
    return await asgi_post(rig.app, body, **kwargs)
