"""Contract section 3: circuit breaker, unit behaviour and effect on requests."""

import asyncio

from gateway.breaker import CircuitBreaker, State
from gateway.config import BreakerConfig, Policy
from gateway.providers import crash, hang, http_error, malformed, ok
from helpers import body_for, make_rig, post, run

CFG = BreakerConfig()  # contract defaults: 2 failures, 5000 ms
SETTLE = 5.1           # request-level sleeps stay clear of float rounding at the boundary


def breaker_of(rig, index=0):
    return rig.app.gateway.routes["standard"][index].breaker


def fail(b, now=0.0):
    b.record_failure(b.acquire(now), now)


def trip(b, now=0.0):
    fail(b, now)
    fail(b, now)


# -- contract defaults ---------------------------------------------------------

def test_defaults_match_the_contract():
    assert CFG.failure_threshold == 2 and CFG.open_duration_s == 5.0


# -- unit: opening and cooldown --------------------------------------------------

def test_unit_one_failure_does_not_open_two_consecutive_do():
    b = CircuitBreaker("p", CFG)
    fail(b)
    assert b.state is State.CLOSED and b.consecutive_failures == 1
    fail(b)
    assert b.state is State.OPEN


def test_unit_cooldown_runs_from_the_failure_that_opened_it():
    b = CircuitBreaker("p", CFG)
    fail(b, 1.0)
    fail(b, 3.0)                               # opens at t=3
    assert b.acquire(7.999) is None
    probe = b.acquire(8.0)
    assert probe is not None and probe.probe and b.state is State.HALF_OPEN


def test_unit_half_open_allows_exactly_one_probe():
    b = CircuitBreaker("p", CFG)
    trip(b)
    assert b.acquire(5.0) is not None
    assert b.acquire(5.0) is None and b.acquire(99.0) is None


# -- unit: counting rules ----------------------------------------------------------

def test_unit_success_resets_the_count():
    b = CircuitBreaker("p", CFG)
    fail(b)
    b.record_success(b.acquire(0.0))
    fail(b)
    assert b.state is State.CLOSED and b.consecutive_failures == 1


def test_unit_non_qualifying_outcome_neither_increments_nor_resets():
    b = CircuitBreaker("p", CFG)
    fail(b)
    b.release(b.acquire(0.0), 0.0)             # non-qualifying: count stays 1
    assert b.consecutive_failures == 1
    fail(b)                                     # so this is the 2nd consecutive failure
    assert b.state is State.OPEN


def test_unit_non_qualifying_does_not_count_as_a_failure_either():
    b = CircuitBreaker("p", CFG)
    for _ in range(10):
        b.release(b.acquire(0.0), 0.0)
    assert b.state is State.CLOSED and b.consecutive_failures == 0


# -- unit: probes ----------------------------------------------------------------

def test_unit_successful_probe_closes_and_resets_count():
    b = CircuitBreaker("p", CFG)
    trip(b)
    b.record_success(b.acquire(5.0))
    assert b.state is State.CLOSED and b.consecutive_failures == 0
    assert b.acquire(5.0) is not None and not b.acquire(5.0).probe


def test_unit_failed_probe_reopens_and_restarts_cooldown_from_the_failure():
    b = CircuitBreaker("p", CFG)
    trip(b)
    b.record_failure(b.acquire(5.0), 5.5)      # probe fails at t=5.5
    assert b.state is State.OPEN
    assert b.acquire(10.4) is None
    assert b.acquire(10.5) is not None


def test_unit_cancelled_or_non_qualifying_probe_releases_slot_and_reopens_with_fresh_cooldown():
    b = CircuitBreaker("p", CFG)
    trip(b)
    probe = b.acquire(5.0)
    b.release(probe, 6.0)                       # cancelled / 4xx / malformed at t=6
    b.release(probe, 7.0)                       # idempotent: must not restart the timer
    assert b.state is State.OPEN                # not half-open, not closed: no recovery claimed
    # White-box: the contract says the slot is released. (Observable behaviour is the same
    # either way, because the open -> half-open transition re-takes the slot; so this is
    # the only way to pin the release itself.)
    assert b._probe_in_flight is False
    assert b.acquire(10.9) is None              # fresh 5000 ms cooldown from t=6
    again = b.acquire(11.0)
    assert again is not None and again.probe


# -- unit: generations / stale completions -------------------------------------------

def test_unit_stale_success_cannot_close_a_circuit_opened_by_newer_failures():
    b = CircuitBreaker("p", CFG)
    old = b.acquire(0.0)                        # admitted while closed
    trip(b, 0.1)                                # newer failures open the circuit
    b.record_success(old)
    assert b.state is State.OPEN
    assert b.acquire(1.0) is None               # still cooling down


def test_unit_stale_success_after_recovery_cannot_reset_a_newer_count():
    b = CircuitBreaker("p", CFG)
    old = b.acquire(0.0)
    trip(b, 0.1)
    b.record_success(b.acquire(5.2))            # probe closes the circuit
    fail(b, 6.0)                                # count = 1 in the new generation
    b.record_success(old)                       # stale: must not reset to 0
    assert b.consecutive_failures == 1
    fail(b, 6.1)
    assert b.state is State.OPEN


def test_unit_stale_failure_cannot_reopen_a_recovered_circuit():
    b = CircuitBreaker("p", CFG)
    old = b.acquire(0.0)
    trip(b, 0.1)
    b.record_success(b.acquire(5.2))
    b.record_failure(old, 6.0)
    assert b.state is State.CLOSED and b.consecutive_failures == 0


def test_unit_stale_failure_while_open_does_not_restart_the_cooldown():
    b = CircuitBreaker("p", CFG)
    old = b.acquire(0.0)
    trip(b, 1.0)                                # opens at t=1
    b.record_failure(old, 4.0)                  # stale; ignored
    assert b.acquire(6.0) is not None           # cooldown still measured from t=1


def test_unit_tickets_settle_once():
    b = CircuitBreaker("p", CFG)
    t = b.acquire(0.0)
    b.record_failure(t, 0.0)
    b.record_failure(t, 0.0)
    assert b.consecutive_failures == 1


def test_unit_transition_events_carry_context_and_reason():
    events = []
    b = CircuitBreaker("p", CFG, events.append)
    ctx = {"request_id": "rid", "attempt": 2, "elapsed_ms": 7, "model": "m"}
    b.record_failure(b.acquire(0.0, ctx), 0.0, ctx)
    b.record_failure(b.acquire(0.0, ctx), 0.0, ctx)
    assert [(e["from"], e["to"], e["reason"]) for e in events] == [
        ("closed", "open", "failure_threshold_reached")
    ]
    assert events[0]["request_id"] == "rid" and events[0]["attempt"] == 2
    assert events[0]["provider"] == "p" and events[0]["elapsed_ms"] == 7


# -- through requests -------------------------------------------------------------------

async def _trip(rig):
    await post(rig)                             # primary fails twice -> circuit opens


def test_circuit_opens_mid_request_and_retry_is_skipped_without_using_an_attempt():
    # Request 1 leaves one failure on the books (retry skipped via a long Retry-After).
    # Request 2's first primary failure is the 2nd consecutive one: the circuit opens
    # and the retry that would have followed is skipped, not dispatched.
    rig = make_rig(
        primary=[http_error(429, retry_after="5"), http_error(503), ok()],
        fallback=[ok()],
    )
    first = run(post(rig)).json
    assert (first["attempts"], first["provider"]) == (2, "fallback")
    assert breaker_of(rig).consecutive_failures == 1
    second = run(post(rig)).json
    assert (second["attempts"], second["provider"]) == (2, "fallback")
    assert rig.primary.call_count == 2
    skipped = rig.of("provider.skipped")
    assert skipped[-1]["provider"] == "primary" and skipped[-1]["reason"] == "circuit_open"
    assert skipped[-1]["attempt"] is None


def test_first_request_two_failures_open_the_circuit_then_primary_is_skipped():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    first = run(post(rig)).json
    assert (first["attempts"], first["provider"]) == (3, "fallback")
    calls = rig.primary.call_count
    second = run(post(rig)).json
    assert (second["attempts"], second["fallback_used"]) == (1, True)
    assert rig.primary.call_count == calls and second["elapsed_ms"] == 0


def test_probe_after_cooldown_gets_exactly_one_call_and_no_retry_on_failure():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])

    async def scenario():
        await _trip(rig)
        before = rig.primary.call_count
        await asyncio.sleep(SETTLE)
        return before, await post(rig)

    before, resp = run(scenario())
    assert rig.primary.call_count == before + 1
    assert (resp.json["attempts"], resp.json["provider"]) == (2, "fallback")
    assert len([e for e in rig.of("attempt.start") if e["probe"]]) == 1


def test_probe_gets_no_retry_even_if_the_breaker_would_admit_another_probe():
    # open_duration 0 makes the breaker willing to admit a second probe immediately, so
    # only the engine's own "no same-provider retry after a probe" rule is left to stop it.
    rig = make_rig(
        primary=[http_error(503)], fallback=[ok()],
        breaker=BreakerConfig(failure_threshold=2, open_duration_s=0.0),
    )

    async def scenario():
        await post(rig)                             # two failures open the circuit
        before = rig.primary.call_count
        return before, await post(rig)              # becomes the probe

    before, second = run(scenario())
    assert rig.primary.call_count == before + 1
    assert second.json["attempts"] == 2 and second.json["provider"] == "fallback"


def test_failed_probe_starts_a_fresh_full_cooldown():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])

    async def scenario():
        await _trip(rig)
        await asyncio.sleep(SETTLE)
        await post(rig)                      # failed probe
        n = rig.primary.call_count
        await asyncio.sleep(4.0)
        await post(rig)                      # still cooling down
        still_open = rig.primary.call_count == n
        await asyncio.sleep(1.2)
        await post(rig)                      # half-open again
        return still_open, rig.primary.call_count - n

    still_open, extra = run(scenario())
    assert still_open and extra == 1


def test_successful_probe_closes_the_circuit():
    rig = make_rig(primary=[http_error(503)] * 2 + [ok()], fallback=[ok()])

    async def scenario():
        await _trip(rig)
        await asyncio.sleep(SETTLE)
        probe = await post(rig)
        after = await post(rig)
        return probe, after

    probe, after = run(scenario())
    assert (probe.json["provider"], probe.json["attempts"]) == ("primary", 1)
    assert (after.json["provider"], after.json["attempts"]) == ("primary", 1)
    assert breaker_of(rig).state is State.CLOSED


def test_concurrent_recovery_admits_exactly_one_probe():
    rig = make_rig(primary=[http_error(503)] * 2 + [ok(delay_s=0.3)], fallback=[ok()])

    async def scenario():
        await _trip(rig)
        await asyncio.sleep(SETTLE)
        tasks = [asyncio.create_task(post(rig)) for _ in range(5)]
        return [await t for t in tasks]

    results = run(scenario())
    providers = sorted(r.json["provider"] for r in results)
    assert providers == ["fallback"] * 4 + ["primary"]
    assert len([e for e in rig.of("attempt.start") if e["probe"]]) == 1
    assert rig.primary.call_count == 3       # 2 failures + the single probe
    skips = [e for e in rig.of("provider.skipped") if e["reason"] == "probe_in_flight"]
    assert len(skips) == 4 and all(e["attempt"] is None for e in skips)


def test_late_completion_cannot_close_a_circuit_opened_by_newer_failures():
    # A starts first and succeeds slowly (400 ms). While it is in flight, B's calls fail
    # twice and open the circuit. A's older success must not close or reset anything.
    rig = make_rig(primary=[ok(delay_s=0.4), http_error(503)], fallback=[ok()])

    async def scenario():
        a = asyncio.create_task(post(rig, body_for(request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")))
        await asyncio.sleep(0)
        b = await post(rig, body_for(request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
        return await a, b, breaker_of(rig).state

    a, b, state_after = run(scenario())
    assert a.status == 200 and a.json["provider"] == "primary"      # A still succeeds
    assert b.json["provider"] == "fallback"
    assert state_after is State.OPEN                                # but did not close it
    assert breaker_of(rig).acquire(rig.primary.call_times[-1] + 1.0) is None


def test_both_providers_fail_gives_503_primary_open_fallback_closed_with_one_failure():
    rig = make_rig(primary=[http_error(503)], fallback=[http_error(503)])
    resp = run(post(rig))
    assert resp.status == 503 and resp.json["error"]["code"] == "providers_unavailable"
    assert resp.json["attempts"] == 3
    assert (rig.primary.call_count, rig.fallback.call_count) == (2, 1)
    assert breaker_of(rig, 0).state is State.OPEN
    assert breaker_of(rig, 1).state is State.CLOSED
    assert breaker_of(rig, 1).consecutive_failures == 1


def test_failed_fallback_ends_the_request_even_with_unused_attempt_capacity():
    # Request 2 skips the open primary, so only 1 of 3 calls is used. The fallback fails
    # and the request still ends (503) even though a second fallback call would succeed.
    rig = make_rig(primary=[http_error(503)], fallback=[ok(), http_error(503), ok()])

    async def scenario():
        await post(rig)                      # primary fails twice -> open; fallback ok (call 1)
        return await post(rig)               # primary skipped; fallback call 2 fails

    resp = run(scenario())
    assert resp.status == 503 and resp.json["attempts"] == 1
    assert rig.fallback.call_count == 2      # call 3 (a success) was never made


def test_both_open_gives_503_with_zero_attempts_and_no_calls():
    rig = make_rig(primary=[http_error(503)], fallback=[http_error(503)])

    async def scenario():
        await post(rig)                      # P open, F failures=1
        await post(rig)                      # P skipped, F failure=2 -> F open
        calls = (rig.primary.call_count, rig.fallback.call_count)
        return calls, await post(rig)

    calls, resp = run(scenario())
    assert resp.status == 503 and resp.json["attempts"] == 0
    assert (rig.primary.call_count, rig.fallback.call_count) == calls


def test_non_qualifying_failures_never_open_the_circuit():
    for step in (http_error(400), http_error(401), http_error(403), malformed(200, {}), crash()):
        rig = make_rig(primary=[step], fallback=[ok()])

        async def many():
            for _ in range(10):
                await post(rig)

        run(many())
        assert rig.primary.call_count == 10, step
        assert breaker_of(rig).state is State.CLOSED


def test_non_qualifying_outcome_between_failures_does_not_reset_the_count():
    # fail (count 1, retry skipped) -> 400 (non-qualifying) -> fail => open.
    rig = make_rig(
        primary=[http_error(429, retry_after="5"), http_error(400), http_error(503)],
        fallback=[ok()],
    )
    run(post(rig)); run(post(rig))
    assert breaker_of(rig).consecutive_failures == 1
    run(post(rig))
    assert breaker_of(rig).state is State.OPEN


def test_success_between_failures_keeps_the_circuit_closed():
    rig = make_rig(primary=[http_error(503), ok()] * 4, fallback=[ok()])

    async def many():
        for _ in range(4):
            await post(rig)

    run(many())
    assert breaker_of(rig).state is State.CLOSED
    assert rig.fallback.call_count == 0


def test_non_qualifying_probe_releases_slot_and_returns_to_open_with_fresh_cooldown():
    rig = make_rig(primary=[http_error(503)] * 2 + [http_error(400), ok()], fallback=[ok()])

    async def scenario():
        await _trip(rig)
        await asyncio.sleep(SETTLE)
        rejected = await post(rig)                 # probe -> 400
        state_after = breaker_of(rig).state
        n = rig.primary.call_count
        immediate = await post(rig)                # fresh cooldown: primary skipped
        skipped = rig.primary.call_count == n
        await asyncio.sleep(SETTLE)
        recovered = await post(rig)                # next probe -> ok
        return rejected, state_after, immediate, skipped, recovered

    rejected, state_after, immediate, skipped, recovered = run(scenario())
    assert rejected.status == 502
    assert state_after is State.OPEN
    assert skipped and immediate.json["provider"] == "fallback"
    assert recovered.json["provider"] == "primary"
    assert breaker_of(rig).state is State.CLOSED


def test_transition_events_are_traced_with_request_id_and_reason():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(_trip(rig))
    t = rig.of("breaker.transition")
    assert [(x["from"], x["to"], x["reason"]) for x in t] == [
        ("closed", "open", "failure_threshold_reached")
    ]
    assert t[0]["request_id"] is not None and t[0]["attempt"] == 2
    assert t[0]["provider"] == "primary" and t[0]["model"] == "stub-primary-1"


def test_circuit_state_is_independent_per_provider():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(post(rig))
    assert breaker_of(rig, 0).state is State.OPEN
    assert breaker_of(rig, 1).state is State.CLOSED
