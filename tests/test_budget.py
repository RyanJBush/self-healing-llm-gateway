"""Contract section 2: total deadline, per-call timeout, Retry-After."""

from datetime import datetime, timezone

import pytest

from gateway.engine import parse_retry_after
from gateway.providers import hang, http_error, ok
from helpers import REQUEST_ID, body_for, make_rig, post, run

NOW = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()  # a Friday


# -- Retry-After parsing -------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0.0), ("1", 1.0), ("120", 120.0), (" 3 ", 3.0),
        ("Fri, 15 Jan 2027 08:00:05 GMT", 5.0),
        ("Fri, 15 Jan 2027 07:00:00 GMT", 0.0),   # past date: valid, zero delay
    ],
)
def test_parse_retry_after_valid(value, expected):
    assert parse_retry_after(value, NOW) == expected


@pytest.mark.parametrize(
    "value",
    [
        None, "", " ", "abc", "-1", "+5", "1.5", "1e3", "0x10", "١٢", "5 seconds",
        "Fri, 30 Feb 2027 08:00:00 GMT",            # impossible date
        "Friday, 15-Jan-27 08:00:05 GMT",           # obsolete RFC 850 form
        "15 Jan 2027 08:00:05 GMT",                 # no weekday
        "Fri, 15 Jan 2027 08:00:05 UTC",            # wrong zone token
        5, 1.5, [], {},
    ],
)
def test_parse_retry_after_invalid(value):
    assert parse_retry_after(value, NOW) is None


def test_parse_retry_after_huge_integer_is_valid_but_never_fits():
    assert parse_retry_after("9" * 5000, NOW) >= 1e9


# -- Retry-After inside a request ------------------------------------------------

def test_valid_retry_after_delays_the_retry_at_least_that_long():
    rig = make_rig(primary=[http_error(429, retry_after="1"), ok()])
    body = run(post(rig)).json
    assert body["attempts"] == 2 and body["provider"] == "primary"
    t = rig.primary.call_times
    assert t[1] - t[0] == pytest.approx(1.0)
    assert body["elapsed_ms"] == 1000


def test_valid_retry_after_below_100ms_still_waits_100ms():
    rig = make_rig(primary=[http_error(503, retry_after="0"), ok()])
    body = run(post(rig)).json
    assert body["elapsed_ms"] == 100


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "", " ", "1e3", "+5", "١٢", "Fri, 30 Feb 2027 08:00:00 GMT"])
def test_invalid_retry_after_uses_100ms(bad):
    rig = make_rig(primary=[http_error(503, retry_after=bad), ok()])
    assert run(post(rig)).json["elapsed_ms"] == 100


def test_absent_retry_after_uses_100ms():
    rig = make_rig(primary=[http_error(503), ok()])
    assert run(post(rig)).json["elapsed_ms"] == 100


def test_retry_after_does_not_fit_deadline_skips_retry_and_falls_back_immediately():
    rig = make_rig(primary=[http_error(429, retry_after="5"), ok()], fallback=[ok()])
    body = run(post(rig)).json
    assert (body["attempts"], body["fallback_used"], body["provider"]) == (2, True, "fallback")
    assert rig.primary.call_count == 1          # no retry
    assert body["elapsed_ms"] == 0              # fallback considered immediately
    assert [e["event"] for e in rig.events if e["event"].startswith("retry")] == ["retry.skipped"]


def test_retry_after_equal_to_remaining_budget_does_not_fit():
    # Wait of exactly 2000 ms would dispatch at the deadline, which is forbidden.
    rig = make_rig(primary=[http_error(503, retry_after="2"), ok()], fallback=[ok()])
    body = run(post(rig)).json
    assert body["provider"] == "fallback" and rig.primary.call_count == 1


def test_retry_after_just_inside_budget_is_honoured():
    # t=0 call 1 returns 503 at 450 ms -> wait 1000 (fits in 1550) -> call 2 at 1450,
    # hangs to 1950 -> fallback is dispatched with only min(500, 50) = 50 ms left.
    rig = make_rig(
        primary=[http_error(503, retry_after="1", delay_s=0.45), hang()],
        fallback=[hang()],
    )
    resp = run(post(rig))
    assert [e["timeout_ms"] for e in rig.of("attempt.start")] == [500, 500, 50]
    assert rig.primary.call_times[1] - rig.primary.call_times[0] == pytest.approx(1.45)
    assert rig.fallback.call_times[0] < 1000.0 + 2.0     # dispatched before the deadline
    assert resp.status == 504 and resp.json["error"]["code"] == "deadline_exceeded"
    assert resp.json["attempts"] == 3 and resp.json["elapsed_ms"] == 2000


def test_nothing_is_dispatched_once_the_deadline_is_reached():
    # Primary's second call times out exactly at the deadline: fallback must not start.
    rig = make_rig(
        primary=[http_error(503, retry_after="1", delay_s=0.45), hang()],
        fallback=[ok()],
        policy=__import__("gateway.config", fromlist=["Policy"]).Policy(per_call_timeout_s=0.55),
    )
    resp = run(post(rig))
    assert resp.status == 504 and resp.json["attempts"] == 2
    assert rig.fallback.call_count == 0


def test_retry_after_http_date_uses_wall_clock():
    soon = "Fri, 15 Jan 2027 08:00:01 GMT"
    rig = make_rig(primary=[http_error(503, retry_after=soon), ok()], wall_clock=lambda: NOW)
    assert run(post(rig)).json["elapsed_ms"] == 1000

    far = "Fri, 01 Jan 2100 00:00:00 GMT"
    rig = make_rig(primary=[http_error(503, retry_after=far), ok()], fallback=[ok()], wall_clock=lambda: NOW)
    assert run(post(rig)).json["provider"] == "fallback"


def test_retry_after_is_per_request_not_remembered():
    rig = make_rig(primary=[http_error(429, retry_after="1"), ok(), http_error(503), ok()])
    assert run(post(rig)).json["elapsed_ms"] == 1000
    assert run(post(rig)).json["elapsed_ms"] == 100


# -- deadline ----------------------------------------------------------------------

def test_deadline_starts_at_handler_entry_so_slow_body_consumes_budget():
    rig = make_rig(primary=[hang()])
    resp = run(post(rig, body_delay=1.9))
    # 100 ms left after reading the body: the call gets min(500, 100).
    assert [e["timeout_ms"] for e in rig.of("attempt.start")] == [100]
    assert resp.status == 504 and resp.json["error"]["code"] == "deadline_exceeded"
    assert resp.json["attempts"] == 1 and resp.json["elapsed_ms"] == 2000
    assert rig.fallback.call_count == 0


@pytest.mark.parametrize("delay", [2.0, 2.5, 60.0])
def test_body_not_complete_by_deadline_is_504_with_zero_calls(delay):
    rig = make_rig()
    resp = run(post(rig, body_delay=delay))
    assert resp.status == 504 and resp.json["error"]["code"] == "deadline_exceeded"
    assert resp.json["attempts"] == 0 and resp.json["elapsed_ms"] == 2000
    assert resp.json["request_id"] is None
    assert rig.primary.call_count == 0


def test_invalid_body_arriving_after_deadline_is_504_not_422():
    rig = make_rig()
    resp = run(post(rig, b"not json", body_delay=2.0))
    assert resp.status == 504


def test_per_call_timeout_is_500ms_when_budget_is_ample():
    rig = make_rig(primary=[hang()], fallback=[ok()])
    run(post(rig))
    assert [e["timeout_ms"] for e in rig.of("attempt.start")] == [500, 500, 500]


def test_total_time_never_exceeds_deadline_even_with_a_slow_stub():
    # A stub slower than its timeout is cut off at the per-call limit.
    rig = make_rig(primary=[ok(delay_s=10)], fallback=[ok(delay_s=10)])
    resp = run(post(rig))
    assert resp.status == 503 and resp.json["elapsed_ms"] == 1600


def test_no_dispatch_at_or_after_deadline_for_any_slow_start():
    for delay_ms in (1500, 1600, 1700, 1800, 1900, 1950, 1999):
        rig = make_rig(primary=[hang()], fallback=[hang()])
        resp = run(post(rig, body_delay=delay_ms / 1000))
        assert resp.json["elapsed_ms"] <= 2000
        for ev in rig.of("attempt.start"):
            assert ev["timeout_ms"] >= 1
        # no provider call began at or after the deadline
        for t in rig.primary.call_times + rig.fallback.call_times:
            assert t < 1000.0 + 2.0
