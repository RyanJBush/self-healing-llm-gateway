"""Trusted configuration. Nothing here is ever read from a request."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    """Budget and retry policy (contract section 2). All times in seconds."""

    total_deadline_s: float = 2.0
    per_call_timeout_s: float = 0.5
    max_calls: int = 3
    primary_max_calls: int = 2
    fallback_max_calls: int = 1
    retry_wait_s: float = 0.1
    max_body_bytes: int = 64 * 1024


@dataclass(frozen=True)
class BreakerConfig:
    """Circuit breaker settings (contract section 3)."""

    failure_threshold: int = 2
    open_duration_s: float = 5.0


# A timer armed for `deadline - now` can fire a float-rounding hair before `deadline`.
# Anything within this margin counts as "at the deadline": nothing is dispatched.
DEADLINE_EPSILON_S = 1e-6

# Request field limits (contract section 1).
INPUT_MIN_CHARS = 1
INPUT_MAX_CHARS = 4000
ROUTE_STANDARD = "standard"
KNOWN_ROUTES = (ROUTE_STANDARD,)

# Route -> ordered provider roles. Resolved to concrete providers in factory.py.
ROUTE_PROVIDER_ORDER = {ROUTE_STANDARD: ("primary", "fallback")}
