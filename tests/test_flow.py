"""Contract section 2: failure classification, retry, fallback, call caps."""

import pytest

from gateway.providers import (
    connection_error, crash, hang, http_error, malformed, ok,
)
from helpers import REQUEST_ID, make_rig, post, run

ERROR_KEYS = {"request_id", "error", "attempts", "elapsed_ms"}


def assert_error(resp, status, code, attempts):
    assert resp.status == status
    body = resp.json
    assert set(body) == ERROR_KEYS
    assert body["error"]["code"] == code
    assert body["attempts"] == attempts
    assert body["request_id"] == REQUEST_ID
    return body


# -- happy path ---------------------------------------------------------------

def test_primary_success_uses_one_call():
    rig = make_rig()
    body = run(post(rig)).json
    assert (body["attempts"], body["fallback_used"], body["provider"]) == (1, False, "primary")
    assert rig.primary.call_count == 1 and rig.fallback.call_count == 0
    assert body["elapsed_ms"] == 0  # virtual clock: nothing waited


def test_stub_output_is_deterministic_and_does_not_echo_input():
    a = run(post(make_rig())).json["output"]
    b = run(post(make_rig())).json["output"]
    assert a == b and "hello" not in a


# -- retryable failures -------------------------------------------------------

RETRYABLE_STATUSES = [408, 429, 500, 501, 502, 503, 504, 599]


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
def test_retryable_status_retries_primary_once_after_100ms(status):
    rig = make_rig(primary=[http_error(status), ok()])
    body = run(post(rig)).json
    assert (body["attempts"], body["fallback_used"], body["provider"]) == (2, False, "primary")
    assert rig.primary.call_count == 2 and rig.fallback.call_count == 0
    assert body["elapsed_ms"] == 100
    t = rig.primary.call_times
    assert t[1] - t[0] == pytest.approx(0.100)


@pytest.mark.parametrize("step", [connection_error(), hang()], ids=["connection", "timeout"])
def test_connection_failure_and_timeout_are_retryable(step):
    rig = make_rig(primary=[step, ok()])
    body = run(post(rig)).json
    assert (body["attempts"], body["provider"]) == (2, "primary")
    expected = 100 if step.kind == "connection_error" else 500 + 100
    assert body["elapsed_ms"] == expected


def test_primary_exhausted_falls_back_with_three_calls():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    body = run(post(rig)).json
    assert (body["attempts"], body["fallback_used"], body["provider"]) == (3, True, "fallback")
    assert body["model"] == "stub-fallback-1"
    assert rig.primary.call_count == 2 and rig.fallback.call_count == 1


def test_all_providers_fail_returns_503_after_exactly_three_calls():
    rig = make_rig(primary=[http_error(503)], fallback=[http_error(500)])
    resp = run(post(rig))
    assert_error(resp, 503, "providers_unavailable", 3)
    assert (rig.primary.call_count, rig.fallback.call_count) == (2, 1)


def test_never_more_than_three_calls_even_when_everything_hangs():
    rig = make_rig(primary=[hang()], fallback=[hang()])
    resp = run(post(rig))
    # 500 + 100 + 500 + 500 = 1600 ms < 2000 ms: three timeouts, then 503.
    body = assert_error(resp, 503, "providers_unavailable", 3)
    assert body["elapsed_ms"] == 1600
    assert (rig.primary.call_count, rig.fallback.call_count) == (2, 1)


def test_no_return_from_fallback_to_primary():
    rig = make_rig(primary=[http_error(503)], fallback=[http_error(503)])
    run(post(rig))
    order = [e["provider"] for e in rig.of("attempt.start")]
    assert order == ["primary", "primary", "fallback"]


def test_fallback_is_never_retried():
    rig = make_rig(primary=[connection_error()], fallback=[http_error(503), ok()])
    resp = run(post(rig))
    assert_error(resp, 503, "providers_unavailable", 3)
    assert rig.fallback.call_count == 1


# -- non-retryable failures ----------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 418, 422, 451])
def test_other_4xx_stops_with_502_no_retry_no_fallback(status):
    rig = make_rig(primary=[http_error(status)], fallback=[ok()])
    resp = run(post(rig))
    body = assert_error(resp, 502, "upstream_rejected", 1)
    assert body["error"]["message"] == "The upstream provider rejected the request."
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_4xx_from_fallback_also_stops_with_502():
    rig = make_rig(primary=[http_error(503)], fallback=[http_error(403)])
    assert_error(run(post(rig)), 502, "upstream_rejected", 3)


MALFORMED = [
    pytest.param(200, None, id="null-body"),
    pytest.param(200, {}, id="empty-object"),
    pytest.param(200, {"output": ""}, id="empty-output"),
    pytest.param(200, {"output": 5}, id="non-string-output"),
    pytest.param(200, {"output": None}, id="null-output"),
    pytest.param(200, {"text": "wrong key"}, id="wrong-key"),
    pytest.param(200, "bare string", id="string-body"),
    pytest.param(200, ["output"], id="list-body"),
    pytest.param(204, None, id="204-no-content"),
    pytest.param(301, {"output": "redirect"}, id="3xx"),
    pytest.param(100, None, id="1xx"),
]


@pytest.mark.parametrize("status,body", MALFORMED)
def test_malformed_success_stops_with_502_no_retry_no_fallback(status, body):
    rig = make_rig(primary=[malformed(status, body)], fallback=[ok()])
    resp = run(post(rig))
    assert_error(resp, 502, "upstream_rejected", 1)
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_unexpected_provider_exception_is_500_with_no_retry():
    rig = make_rig(primary=[crash()], fallback=[ok()])
    resp = run(post(rig))
    assert_error(resp, 500, "internal_error", 1)
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


# -- sanitization ----------------------------------------------------------------

def test_upstream_text_and_internal_exceptions_never_reach_the_client():
    leaks = [
        (http_error(403, body={"error": "UPSTREAM-SECRET-403"}), "UPSTREAM-SECRET-403"),
        (malformed(200, {"output": 5, "debug": "UPSTREAM-SECRET-MAL"}), "UPSTREAM-SECRET-MAL"),
        (crash(), "secret-internal-detail"),
    ]
    for step, marker in leaks:
        resp = run(post(make_rig(primary=[step])))
        assert marker not in resp.raw.decode()
        assert set(resp.json["error"]) == {"code", "message"}


def test_input_text_is_never_logged_in_events():
    rig = make_rig(primary=[http_error(503), ok()])
    run(post(rig, __import__("helpers").body_for(input="PRIVATE-PROMPT-TEXT")))
    assert "PRIVATE-PROMPT-TEXT" not in repr(rig.events)


# -- events ----------------------------------------------------------------------

def test_every_event_carries_the_request_id():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(post(rig))
    assert rig.events
    assert {e["request_id"] for e in rig.events} == {REQUEST_ID}
    assert rig.names()[-1] == "request.finished"
    assert rig.names().count("attempt.start") == 3


def test_concurrent_requests_have_independent_budgets():
    import asyncio

    from helpers import body_for

    rig = make_rig(primary=[ok(delay_s=0.3)])

    async def both():
        a = post(rig, body_for(request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
        b = post(rig, body_for(request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
        return await asyncio.gather(a, b)

    ra, rb = run(both())
    assert ra.json["request_id"].startswith("aaaa") and rb.json["request_id"].startswith("bbbb")
    assert ra.json["elapsed_ms"] == rb.json["elapsed_ms"] == 300


# -- reviewer concern: a non-retryable 4xx must be proven by behaviour, not wording ------

NON_RETRYABLE_4XX = [c for c in range(400, 500) if c not in (408, 429)]


@pytest.mark.parametrize("status", NON_RETRYABLE_4XX)
def test_every_4xx_except_408_and_429_is_502_one_call_no_fallback_no_wait(status):
    rig = make_rig(primary=[http_error(status)], fallback=[ok()])
    resp = run(post(rig))
    body = assert_error(resp, 502, "upstream_rejected", 1)
    assert rig.primary.call_count == 1                      # exactly one provider call
    assert rig.fallback.call_count == 0                     # zero fallback calls
    assert not {"retry.wait", "retry.skipped"} & set(rig.names())   # no retry wait
    assert body["elapsed_ms"] == 0                          # nothing waited
    assert len(rig.of("attempt.start")) == 1


@pytest.mark.parametrize("status", [400, 401, 403])
def test_representative_rejections_never_touch_the_circuit(status):
    rig = make_rig(primary=[http_error(status)], fallback=[ok()])
    for _ in range(5):
        run(post(rig))
    assert rig.primary.call_count == 5 and rig.fallback.call_count == 0
    assert rig.of("breaker.transition") == []


@pytest.mark.parametrize("status", [408, 429])
def test_408_and_429_are_the_only_retryable_4xx(status):
    rig = make_rig(primary=[http_error(status), ok()])
    body = run(post(rig)).json
    assert body["attempts"] == 2 and "retry.wait" in rig.names()


# -- injected sleeper / separate clocks ---------------------------------------------------

def test_retry_wait_that_stalls_until_the_deadline_never_dispatches_again():
    import asyncio

    async def stalled_sleep(_seconds):
        await asyncio.sleep(1.5)        # 500 ms timeout + 1500 ms = exactly the deadline

    rig = make_rig(primary=[hang()], fallback=[ok()], sleep=stalled_sleep)
    resp = run(post(rig))
    body = assert_error(resp, 504, "deadline_exceeded", 1)
    assert body["elapsed_ms"] == 2000
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_retry_wait_that_overshoots_the_deadline_never_dispatches_again():
    import asyncio

    async def overshooting_sleep(_seconds):
        await asyncio.sleep(2.5)

    rig = make_rig(primary=[hang()], fallback=[ok()], sleep=overshooting_sleep)
    resp = run(post(rig))
    assert_error(resp, 504, "deadline_exceeded", 1)
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_wall_clock_cannot_move_the_monotonic_deadline():
    ticks = iter(range(10**9))          # a wall clock that lurches 1e6 s on every read
    rig = make_rig(primary=[hang()], fallback=[hang()], wall_clock=lambda: next(ticks) * 1e6)
    resp = run(post(rig))
    assert resp.json["elapsed_ms"] == 1600 and resp.status == 503


def test_http_date_retry_after_uses_wall_clock_only():
    from datetime import datetime, timezone

    now = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    date = "Fri, 15 Jan 2027 08:00:01 GMT"
    # Monotonic loop time starts at 1000.0 s (nothing like epoch time); the delay must
    # still be exactly date - wall_clock = 1 s.
    rig = make_rig(primary=[http_error(503, retry_after=date), ok()], wall_clock=lambda: now)
    assert run(post(rig)).json["elapsed_ms"] == 1000
    # A wall clock far in the past makes the same date a huge delay: retry is skipped,
    # fallback considered immediately, deadline untouched.
    rig = make_rig(primary=[http_error(503, retry_after=date), ok()], fallback=[ok()],
                   wall_clock=lambda: 0.0)
    body = run(post(rig)).json
    assert body["provider"] == "fallback" and body["elapsed_ms"] == 0
