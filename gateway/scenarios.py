"""Scenario-keyed stubs for the deterministic acceptance sequence (contract section 4).

Failure behaviour is keyed by (scenario, request ID, provider, provider-local attempt
number). There is no shared outcome queue, so the gateway (which makes retries, fallbacks
and skips) and the direct-primary baseline (one call per request) can replay the same
schedule and each get the answer that belongs to the call they actually make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .providers import CallContext, RawResponse, Step, http_error, ok, play_step

CALL_MS = 10  # every completed stub call takes exactly 10 simulated ms

# Fixed, valid request IDs for the labels R1..R7 (and a few extras for other cases).
RID = {
    f"R{i}": f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 8)
}

Key = tuple[str, str, int]  # (request_id, provider_id, provider_attempt)


@dataclass(frozen=True)
class Scenario:
    name: str
    # Only failures are listed; every other call succeeds after CALL_MS.
    failures: Mapping[Key, Step] = field(default_factory=dict)

    def step_for(self, request_id: str, provider_id: str, attempt: int) -> Step:
        return self.failures.get((request_id, provider_id, attempt), ok(CALL_MS / 1000))


def fail_503() -> Step:
    return http_error(503, delay_s=CALL_MS / 1000)


def acceptance_scenario() -> Scenario:
    """R1..R7 from the contract. Entries for calls the gateway will not make (R4, R6
    primary) are still listed: the baseline does make them."""
    primary_failures = [
        (RID["R2"], "primary", 1),
        (RID["R3"], "primary", 1),
        (RID["R3"], "primary", 2),
        (RID["R4"], "primary", 1),
        (RID["R5"], "primary", 1),
        (RID["R6"], "primary", 1),
    ]
    return Scenario("acceptance-R1-R7", {k: fail_503() for k in primary_failures})


class ScheduledStubProvider:
    execution_mode = "stub"

    def __init__(self, provider_id: str, model: str, scenario: Scenario):
        self.id = provider_id
        self.model = model
        self.scenario = scenario
        self.calls: list[tuple[str, int]] = []  # (request_id, provider_attempt)

    async def call(self, text: str, ctx: CallContext) -> RawResponse:
        self.calls.append((ctx.request_id, ctx.provider_attempt))
        step = self.scenario.step_for(ctx.request_id, self.id, ctx.provider_attempt)
        return await play_step(step, self.id, text)
