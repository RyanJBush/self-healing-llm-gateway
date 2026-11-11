"""Retry, fallback and deadline engine (contract section 2).

Clocks are separate and both injectable:

* monotonic time = the running event loop's clock (``loop.time()``). Run the gateway on
  ``gateway.simclock.SimulatedLoop`` to control it. The request deadline uses only this.
* wall-clock time = ``wall_clock`` (default ``time.time``). Used only to turn an HTTP-date
  ``Retry-After`` into a delay. It can jump or be wrong without touching the deadline.
* the retry-wait sleeper = ``sleep`` (default ``asyncio.sleep``). A test can make it
  overshoot to prove the deadline is rechecked after every wait.

Invariants enforced here:

* the deadline is rechecked before every wait and every dispatch;
* a call is never dispatched at or after the deadline;
* at most ``max_calls`` calls total, primary <= 2, fallback <= 1;
* a failed fallback ends the request even if the global cap has capacity;
* open or occupied half-open providers are skipped without using an attempt;
* a request never returns from fallback to primary;
* no retry beyond the table in section 2.

A timeout does not prove the remote work stopped. A retry or fallback after a timeout
can duplicate work or billing. The gateway never claims exactly-once execution.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .breaker import CircuitBreaker
from .config import DEADLINE_EPSILON_S, Policy
from .envelope import (
    MESSAGE_FOR_CODE,
    MESSAGE_UPSTREAM_UNUSABLE,
    REASON_FOR_CODE,
    STATUS_FOR_CODE,
    error_body,
    success_body,
)
from .providers import CallContext, ConnectionFailure, Provider, RawResponse
from .safelog import describe
from .validation import ParsedRequest

log = logging.getLogger("gateway.engine")

Sink = Callable[[dict], None]

TIMEOUT_NOTE = (
    "Timeout: the provider outcome is uncertain. Cancelling locally does not prove the "
    "remote work stopped; a retry or fallback can duplicate work or billing."
)


@dataclass
class Slot:
    """A provider plus its circuit breaker, in route order."""

    provider: Provider
    breaker: CircuitBreaker


@dataclass(frozen=True)
class Outcome:
    status: int
    body: dict[str, Any]
    code: str | None  # error code, or None on success
    attempts: int
    reason: str = "completed"


class Kind(enum.Enum):
    SUCCESS = "success"
    RETRYABLE = "qualifying_failure"  # counts toward the circuit
    REJECTED = "non_qualifying_rejected"  # other 4xx: stop
    MALFORMED = "non_qualifying_malformed"  # bad success body / unexpected status: stop


@dataclass(frozen=True)
class CallResult:
    kind: Kind
    detail: str
    output: str | None = None
    retry_after_s: float | None = None
    uncertain: bool = False  # remote outcome unknown (timeouts)


@dataclass
class _State:
    attempts: int = 0
    fallback_used: bool = False
    primary_skipped: bool = False


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_HTTP_DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) (" + "|".join(_MONTHS) + r") (\d{4}) "
    r"(\d{2}):(\d{2}):(\d{2}) GMT"
)
_HUGE_SECONDS = 1e10  # a valid but absurdly long delay; can never fit a deadline


def parse_retry_after(value: Any, now_epoch: float) -> float | None:
    """Seconds to wait, or ``None`` if the value is absent or invalid.

    Valid: non-negative integer seconds, or an IMF-fixdate HTTP-date. ``now_epoch`` is
    wall-clock time; it is never mixed with the monotonic request deadline.
    """
    if not isinstance(value, str):
        return None
    v = value.strip(" \t")
    if re.fullmatch(r"[0-9]+", v):
        return _HUGE_SECONDS if len(v) > 10 else float(int(v))
    m = _HTTP_DATE_RE.fullmatch(v)
    if m is None:
        return None
    day, mon, year, hh, mm, ss = m.groups()
    try:
        when = datetime(
            int(year), _MONTHS[mon], int(day), int(hh), int(mm), int(ss), tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return max(0.0, when.timestamp() - now_epoch)


def _header(headers: Any, name: str) -> Any:
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Gateway:
    def __init__(
        self,
        routes: Mapping[str, list[Slot]],
        policy: Policy,
        *,
        sink: Sink | None = None,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.routes = routes
        self.policy = policy
        self._sink = sink
        self._wall_clock = wall_clock
        self._sleep = sleep

    # -- events ------------------------------------------------------------

    def emit(
        self,
        event: str,
        request_id: str | None,
        start: float,
        *,
        slot: Slot | None = None,
        attempt: int | None = None,
        outcome: str | None = None,
        reason: str | None = None,
        **extra: Any,
    ) -> None:
        """Trace event. Never carries prompts, outputs, credentials, or upstream bodies."""
        if self._sink is None:
            return
        try:
            self._sink(
                {
                    "event": event,
                    "request_id": request_id,
                    "attempt": attempt,  # dispatched calls only; null for skips
                    "provider": slot.provider.id if slot else None,
                    "model": slot.provider.model if slot else None,
                    "elapsed_ms": self._ms(start),
                    "outcome": outcome,
                    "reason": reason,
                    **extra,
                }
            )
        except Exception:  # an event sink must never break a request
            pass

    def _breaker_ctx(self, rid: str, start: float, slot: Slot, attempt: int | None) -> dict:
        return {
            "request_id": rid,
            "attempt": attempt,
            "model": slot.provider.model,
            "elapsed_ms": self._ms(start),
        }

    # -- public ------------------------------------------------------------

    async def execute(self, req: ParsedRequest, start: float, deadline: float) -> Outcome:
        state = _State()
        try:
            return await self._run(req, start, deadline, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Defect. The operator log gets the exception type and call-site frames only:
            # exception text can hold the prompt, credentials, or upstream content.
            log.error("unexpected gateway exception request_id=%s %s", req.request_id, describe(exc))
            return self._error(req.request_id, "internal_error", start, state.attempts)

    # -- internals ---------------------------------------------------------

    def _ms(self, start: float) -> int:
        return round((asyncio.get_running_loop().time() - start) * 1000)

    def _error(
        self,
        request_id: str | None,
        code: str,
        start: float,
        attempts: int,
        message: str | None = None,
    ) -> Outcome:
        body = error_body(
            request_id,
            code,
            message or MESSAGE_FOR_CODE[code],
            attempts,
            self._ms(start),
        )
        return Outcome(STATUS_FOR_CODE[code], body, code, attempts, REASON_FOR_CODE[code])

    async def _run(
        self, req: ParsedRequest, start: float, deadline: float, st: _State
    ) -> Outcome:
        loop = asyncio.get_running_loop()
        p = self.policy
        rid = req.request_id

        for idx, slot in enumerate(self.routes[req.route]):
            role = "primary" if idx == 0 else "fallback"
            max_here = p.primary_max_calls if idx == 0 else p.fallback_max_calls
            calls_here = 0

            while True:
                now = loop.time()
                if now >= deadline - DEADLINE_EPSILON_S:  # recheck before every dispatch
                    return self._error(rid, "deadline_exceeded", start, st.attempts)
                if calls_here >= max_here or st.attempts >= p.max_calls:
                    break
                ticket = slot.breaker.acquire(now, self._breaker_ctx(rid, start, slot, None))
                if ticket is None:
                    if idx == 0:
                        st.primary_skipped = True
                    self.emit(
                        "provider.skipped",
                        rid,
                        start,
                        slot=slot,
                        outcome="skipped",
                        reason=(
                            "circuit_open"
                            if slot.breaker.state.value == "open"
                            else "probe_in_flight"
                        ),
                        role=role,
                        circuit=slot.breaker.state.value,
                    )
                    break

                st.attempts += 1
                calls_here += 1
                if idx > 0:
                    st.fallback_used = True
                timeout = min(p.per_call_timeout_s, deadline - now)
                if ticket.probe:
                    why = "half_open_probe"
                elif idx == 0:
                    why = "primary_initial" if calls_here == 1 else "primary_retry"
                else:
                    why = (
                        "fallback_primary_skipped"
                        if st.primary_skipped
                        else "fallback_primary_failed"
                    )
                self.emit(
                    "attempt.start",
                    rid,
                    start,
                    slot=slot,
                    attempt=st.attempts,
                    outcome="dispatched",
                    reason=why,
                    role=role,
                    provider_attempt=calls_here,
                    probe=ticket.probe,
                    timeout_ms=round(timeout * 1000),
                )
                bctx = self._breaker_ctx(rid, start, slot, st.attempts)
                try:
                    result = await self._call(
                        slot.provider, req.input, timeout, CallContext(rid, calls_here, st.attempts)
                    )
                    if result.kind is Kind.SUCCESS:
                        slot.breaker.record_success(ticket, bctx)
                    elif result.kind is Kind.RETRYABLE:
                        slot.breaker.record_failure(ticket, loop.time(), self._refresh(bctx, start))
                except asyncio.CancelledError:
                    self.emit(
                        "attempt.end",
                        rid,
                        start,
                        slot=slot,
                        attempt=st.attempts,
                        outcome="cancelled",
                        reason="client_disconnected_or_shutdown",
                        remote_outcome="uncertain",
                        note="Local cancellation does not prove remote work stopped.",
                    )
                    raise
                finally:
                    # Non-qualifying end or cancellation: frees a probe slot and re-opens
                    # the circuit with a fresh cooldown. No-op once settled above.
                    slot.breaker.release(ticket, loop.time(), self._refresh(bctx, start))
                self.emit(
                    "attempt.end",
                    rid,
                    start,
                    slot=slot,
                    attempt=st.attempts,
                    outcome=result.kind.value,
                    reason=result.detail,
                    remote_outcome="uncertain" if result.uncertain else "known",
                    **({"note": TIMEOUT_NOTE} if result.uncertain else {}),
                )

                if result.kind is Kind.SUCCESS:
                    assert result.output is not None
                    body = success_body(
                        request_id=rid,
                        output=result.output,
                        provider=slot.provider.id,
                        model=slot.provider.model,
                        execution_mode=slot.provider.execution_mode,
                        attempts=st.attempts,
                        fallback_used=st.fallback_used,
                        elapsed_ms=self._ms(start),
                    )
                    return Outcome(200, body, None, st.attempts, "completed")

                if result.kind in (Kind.REJECTED, Kind.MALFORMED):
                    message = (
                        MESSAGE_FOR_CODE["upstream_rejected"]
                        if result.kind is Kind.REJECTED
                        else MESSAGE_UPSTREAM_UNUSABLE
                    )
                    return self._error(rid, "upstream_rejected", start, st.attempts, message)

                # Qualifying failure. Same-provider retry only for the primary, once,
                # never after a half-open probe. A failed fallback ends the request.
                if idx != 0 or ticket.probe or calls_here >= max_here:
                    break
                wait = max(p.retry_wait_s, result.retry_after_s or 0.0)
                remaining = deadline - loop.time()
                if wait >= remaining:
                    self.emit(
                        "retry.skipped",
                        rid,
                        start,
                        slot=slot,
                        outcome="skipped",
                        reason="wait_does_not_fit_deadline",
                        wait_ms=round(min(wait, 1e7) * 1000),
                        remaining_ms=max(0, round(remaining * 1000)),
                    )
                    break
                self.emit(
                    "retry.wait",
                    rid,
                    start,
                    slot=slot,
                    outcome="waiting",
                    reason="retry_after" if result.retry_after_s else "default_retry_wait",
                    wait_ms=round(wait * 1000),
                )
                # The wait itself is bounded by the absolute deadline: a sleeper that
                # stalls or never returns is cancelled at the deadline, not awaited.
                bound = asyncio.timeout_at(deadline)
                not_before = loop.time() + wait
                try:
                    async with bound:
                        await self._sleep(wait)
                        # Event-loop timers may wake one clock-resolution early.
                        # Preserve the minimum delay, including Retry-After, while
                        # retaining the same absolute deadline for every re-wait.
                        while loop.time() < not_before:
                            await self._sleep(not_before - loop.time())
                except TimeoutError:
                    if not bound.expired():
                        raise  # the sleeper's own error: a defect, not our deadline
                    self.emit(
                        "retry.wait_cut_off",
                        rid,
                        start,
                        slot=slot,
                        outcome="cancelled",
                        reason="deadline_reached_while_waiting",
                    )
                    return self._error(rid, "deadline_exceeded", start, st.attempts)

        if loop.time() >= deadline - DEADLINE_EPSILON_S:
            return self._error(rid, "deadline_exceeded", start, st.attempts)
        return self._error(rid, "providers_unavailable", start, st.attempts)

    def _refresh(self, ctx: dict, start: float) -> dict:
        return {**ctx, "elapsed_ms": self._ms(start)}

    async def _call(
        self, provider: Provider, text: str, timeout: float, ctx: CallContext
    ) -> CallResult:
        try:
            async with asyncio.timeout(timeout):
                raw = await provider.call(text, ctx)
        except TimeoutError:
            return CallResult(Kind.RETRYABLE, "timeout", uncertain=True)
        except ConnectionFailure:
            return CallResult(Kind.RETRYABLE, "connection_failure")
        return self._classify(raw)

    def _classify(self, raw: Any) -> CallResult:
        if not isinstance(raw, RawResponse):
            return CallResult(Kind.MALFORMED, "not_a_response")
        status = raw.status
        if isinstance(status, bool) or not isinstance(status, int):
            return CallResult(Kind.MALFORMED, "bad_status")
        if 200 <= status < 300:
            body = raw.body
            if isinstance(body, dict):
                output = body.get("output")
                if isinstance(output, str) and output != "":
                    return CallResult(Kind.SUCCESS, f"http_{status}", output=output)
            return CallResult(Kind.MALFORMED, "malformed_success_body")
        # Among 4xx only 408 and 429 are retryable; every 5xx is.
        if status in (408, 429) or 500 <= status <= 599:
            retry_after = parse_retry_after(_header(raw.headers, "retry-after"), self._wall_clock())
            return CallResult(Kind.RETRYABLE, f"http_{status}", retry_after_s=retry_after)
        if 400 <= status < 500:
            return CallResult(Kind.REJECTED, f"http_{status}")
        return CallResult(Kind.MALFORMED, f"unexpected_status_{status}")
