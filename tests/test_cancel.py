"""Client disconnect / cancellation and gateway-defect handling."""

import asyncio

from gateway.breaker import State
from gateway.providers import hang, http_error, ok
from helpers import make_rig, post, run


def breaker_of(rig, index=0):
    return rig.app.gateway.routes["standard"][index].breaker


def test_disconnect_during_call_cancels_work_and_makes_no_further_calls():
    rig = make_rig(primary=[hang()], fallback=[ok()])

    async def scenario():
        resp = await post(rig, disconnect_at=0.2)
        await asyncio.sleep(10)              # plenty of time for any stray retry
        return resp

    resp = run(scenario())
    assert resp.status is None and resp.sent == []        # nothing written
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)
    cancelled = rig.of("request.cancelled")
    assert len(cancelled) == 1 and cancelled[0]["phase"] == "execute"
    assert "request.finished" not in rig.names()


def test_disconnect_during_retry_wait_makes_no_second_call():
    rig = make_rig(primary=[http_error(503), ok()], fallback=[ok()])

    async def scenario():
        resp = await post(rig, disconnect_at=0.05)        # inside the 100 ms wait
        await asyncio.sleep(10)
        return resp

    assert run(scenario()).status is None
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_disconnect_while_reading_body_makes_no_calls():
    rig = make_rig()
    resp = run(post(rig, body_delay=0.5, disconnect_at=0.1))
    assert resp.status is None
    assert rig.primary.call_count == 0
    assert rig.of("request.cancelled")[0]["phase"] == "body"


def test_cancellation_is_not_a_circuit_failure():
    rig = make_rig(primary=[hang()] * 4 + [ok()], fallback=[ok()])

    async def scenario():
        for _ in range(4):                    # more than the failure threshold
            await post(rig, disconnect_at=0.1)
        return await post(rig)

    resp = run(scenario())
    assert breaker_of(rig).state is State.CLOSED
    assert (resp.json["provider"], resp.json["attempts"]) == ("primary", 1)


def test_cancelled_probe_releases_slot_and_reopens_with_fresh_cooldown():
    rig = make_rig(primary=[http_error(503)] * 2 + [hang(), ok()], fallback=[ok()])

    async def scenario():
        await post(rig)                                   # two failures open the circuit
        await asyncio.sleep(5.1)
        gone = await post(rig, disconnect_at=0.2)         # probe hangs, client leaves
        state_after = breaker_of(rig).state
        n = rig.primary.call_count
        soon = await post(rig)                            # fresh cooldown: primary skipped
        skipped = rig.primary.call_count == n
        await asyncio.sleep(5.1)
        again = await post(rig)                           # slot was freed: new probe
        return gone, state_after, soon, skipped, again

    gone, state_after, soon, skipped, again = run(scenario())
    assert gone.status is None
    assert state_after is State.OPEN                      # recovery not claimed
    assert skipped and soon.json["provider"] == "fallback"
    assert (again.json["provider"], again.json["attempts"]) == ("primary", 1)
    assert breaker_of(rig).state is State.CLOSED
    reasons = [e["reason"] for e in rig.of("breaker.transition")]
    assert "probe_not_conclusive" in reasons


def test_cancelled_call_is_traced_as_uncertain_remote_outcome():
    rig = make_rig(primary=[hang()])
    run(post(rig, disconnect_at=0.2))
    end = [e for e in rig.of("attempt.end") if e["outcome"] == "cancelled"]
    assert len(end) == 1 and end[0]["remote_outcome"] == "uncertain"


# -- internal errors ----------------------------------------------------------------

def test_event_sink_failure_cannot_break_a_request():
    rig = make_rig()

    def boom(_event):
        raise RuntimeError("sink down")

    rig.app.gateway._sink = boom
    for slot in rig.app.gateway.routes["standard"]:
        slot.breaker._sink = boom
    assert run(post(rig)).status == 200


def test_defect_in_engine_is_500_without_retry_and_without_leaking(monkeypatch):
    rig = make_rig(fallback=[ok()])

    def explode(self, raw):
        raise ValueError("SECRET-ENGINE-DETAIL")

    monkeypatch.setattr(type(rig.app.gateway), "_classify", explode)
    resp = run(post(rig))
    assert resp.status == 500
    body = resp.json
    assert body["error"] == {"code": "internal_error", "message": "Internal gateway error."}
    assert body["attempts"] == 1 and body["request_id"] is not None
    assert "SECRET-ENGINE-DETAIL" not in resp.raw.decode()
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)
    assert breaker_of(rig).consecutive_failures == 0
