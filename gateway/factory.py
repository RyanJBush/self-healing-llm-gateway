"""Wire config + providers + breakers into a Gateway / ASGI app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from .app import GatewayApp
from .breaker import CircuitBreaker
from .config import ROUTE_PROVIDER_ORDER, BreakerConfig, Policy
from .engine import Gateway, Slot
from .providers import Provider, StubProvider, step_from_dict

events_log = logging.getLogger("gateway.events")


def log_event(event: dict) -> None:
    """Default sink: one JSON object per line on logger ``gateway.events``.
    Events never contain input text or upstream bodies."""
    events_log.info(json.dumps(event, separators=(",", ":"), sort_keys=True))


def build_gateway(
    primary: Provider,
    fallback: Provider,
    *,
    policy: Policy | None = None,
    breaker: BreakerConfig | None = None,
    sink: Callable[[dict], None] | None = log_event,
    wall_clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Gateway:
    policy = policy or Policy()
    breaker = breaker or BreakerConfig()
    by_role = {"primary": primary, "fallback": fallback}
    routes = {
        route: [
            Slot(by_role[role], CircuitBreaker(by_role[role].id, breaker, sink))
            for role in roles
        ]
        for route, roles in ROUTE_PROVIDER_ORDER.items()
    }
    return Gateway(routes, policy, sink=sink, wall_clock=wall_clock, sleep=sleep)


def build_app(gateway: Gateway) -> GatewayApp:
    return GatewayApp(gateway)


def stub_providers_from_env(env: dict[str, str] | None = None) -> tuple[StubProvider, StubProvider]:
    """Healthy stubs, or scripted ones from the operator's ``GATEWAY_STUB_SCENARIO`` file:
    ``{"primary": [{"kind": "http", "status": 503}], "fallback": [{"kind": "ok"}]}``.
    """
    env = os.environ if env is None else env
    scripts: dict[str, list] = {"primary": [], "fallback": []}
    path = env.get("GATEWAY_STUB_SCENARIO")
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for role in scripts:
            scripts[role] = [step_from_dict(s) for s in data.get(role, [])]
    return (
        StubProvider("primary", "stub-primary-1", scripts["primary"]),
        StubProvider("fallback", "stub-fallback-1", scripts["fallback"]),
    )


def create_default_app() -> GatewayApp:
    primary, fallback = stub_providers_from_env()
    return build_app(build_gateway(primary, fallback))
