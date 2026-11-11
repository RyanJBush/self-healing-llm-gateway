"""Per-provider circuit breaker (contract section 3).

State is process-local and in memory. A lock guards state and probe admission and is
held only while state is read or changed; events are emitted after it is released.

Generations: every state change bumps ``generation``. A ticket remembers the generation
it was admitted under. A completion whose ticket is from an older generation is stale and
is ignored, so an old in-flight success can never close a circuit that newer failures
opened, and an old failure can never re-open one that has since recovered.
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .config import BreakerConfig

Sink = Callable[[dict], None]


class State(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(eq=False)
class Ticket:
    """Permission for exactly one provider call. Settled exactly once."""

    probe: bool
    generation: int
    settled: bool = field(default=False, repr=False)


class CircuitBreaker:
    def __init__(self, name: str, config: BreakerConfig, sink: Sink | None = None):
        self.name = name
        self._config = config
        self._sink = sink
        self._lock = threading.Lock()
        self.state = State.CLOSED
        self.consecutive_failures = 0
        self.generation = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    # -- admission ---------------------------------------------------------

    def acquire(self, now: float, ctx: Mapping[str, Any] | None = None) -> Ticket | None:
        """Return a ticket, or ``None`` if the provider must be skipped.

        At cooldown expiry the first caller atomically takes the single probe slot and
        moves open -> half-open; everyone else is skipped while the probe runs.
        """
        events: list[dict] = []
        with self._lock:
            ticket: Ticket | None = None
            if self.state is State.CLOSED:
                ticket = Ticket(False, self.generation)
            elif self.state is State.OPEN:
                if now - self._opened_at >= self._config.open_duration_s:
                    self._move(State.HALF_OPEN, "cooldown_elapsed", events)
                    self._probe_in_flight = True
                    ticket = Ticket(True, self.generation)
            elif not self._probe_in_flight:  # HALF_OPEN with a free slot
                self._probe_in_flight = True
                ticket = Ticket(True, self.generation)
        self._flush(events, ctx)
        return ticket

    # -- settlement (call exactly one of these per ticket) -----------------

    def record_success(self, ticket: Ticket, ctx: Mapping[str, Any] | None = None) -> None:
        events: list[dict] = []
        with self._lock:
            if self._settle(ticket) and ticket.generation == self.generation:
                if ticket.probe:
                    self._probe_in_flight = False
                    self.consecutive_failures = 0
                    self._move(State.CLOSED, "probe_succeeded", events)
                else:
                    self.consecutive_failures = 0
        self._flush(events, ctx)

    def record_failure(
        self, ticket: Ticket, now: float, ctx: Mapping[str, Any] | None = None
    ) -> None:
        """A qualifying failure: connection failure, timeout, 408, 429, or 5xx."""
        events: list[dict] = []
        with self._lock:
            if self._settle(ticket) and ticket.generation == self.generation:
                if ticket.probe:
                    self._probe_in_flight = False
                    self._opened_at = now
                    self._move(State.OPEN, "probe_failed", events)
                else:  # generation matches, so the circuit is closed
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= self._config.failure_threshold:
                        self._opened_at = now
                        self._move(State.OPEN, "failure_threshold_reached", events)
        self._flush(events, ctx)

    def release(
        self, ticket: Ticket, now: float, ctx: Mapping[str, Any] | None = None
    ) -> None:
        """Neutral end: non-qualifying outcome, cancellation, or internal error.

        Closed-circuit calls change nothing (the failure count is neither incremented nor
        reset). A probe frees its slot and the circuit returns to open with a fresh
        cooldown: recovery is not claimed. Idempotent.
        """
        events: list[dict] = []
        with self._lock:
            if self._settle(ticket) and ticket.probe and ticket.generation == self.generation:
                self._probe_in_flight = False
                self._opened_at = now
                self._move(State.OPEN, "probe_not_conclusive", events)
        self._flush(events, ctx)

    # -- internals (lock held) ---------------------------------------------

    @staticmethod
    def _settle(ticket: Ticket) -> bool:
        if ticket.settled:
            return False
        ticket.settled = True
        return True

    def _move(self, new: State, reason: str, events: list[dict]) -> None:
        old, self.state = self.state, new
        self.generation += 1
        events.append(
            {
                "event": "breaker.transition",
                "provider": self.name,
                "from": old.value,
                "to": new.value,
                "reason": reason,
                "generation": self.generation,
                "outcome": new.value,
            }
        )

    def _flush(self, events: list[dict], ctx: Mapping[str, Any] | None) -> None:
        if self._sink is None:
            return
        for event in events:
            try:
                self._sink({**(ctx or {}), **event})
            except Exception:  # an event sink must never break a request
                pass
