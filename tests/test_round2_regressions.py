"""Regressions from the second independent review.

1. The retry wait itself must be bounded by the absolute deadline.
2. Unexpected-exception logs must not contain exception text (prompt, credentials).
"""

import asyncio
import logging
import time

import pytest

from gateway.config import Policy
from gateway.factory import build_app, build_gateway
from gateway.providers import StubProvider, crash, hang, http_error, ok
from helpers import body_for, make_rig, post, run

PROMPT = "PROMPT-PLEASE-DO-NOT-LOG-ME"
CRED = "sk-live-SYNTHETIC-CREDENTIAL-12345"


# -- 1. bounded retry wait ----------------------------------------------------------

def test_sleeper_that_takes_60s_is_cut_off_at_the_deadline():
    cancelled = []

    async def sixty_seconds(_wait):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    rig = make_rig(primary=[hang()], fallback=[ok()], sleep=sixty_seconds)

    async def scenario():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        resp = await post(rig)
        return resp, loop.time() - t0

    resp, took = run(scenario())
    body = resp.json
    assert resp.status == 504 and body["error"]["code"] == "deadline_exceeded"
    assert body["elapsed_ms"] == 2000                 # was 60000 before the fix
    assert took == pytest.approx(2.0)                 # total simulated time, not 60 s
    assert body["attempts"] == 1
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)
    assert cancelled == [True]                        # the sleeper was cancelled, not abandoned
    assert rig.of("retry.wait_cut_off")[0]["reason"] == "deadline_reached_while_waiting"


def test_sleeper_that_never_completes_returns_504_at_the_deadline():
    state = {"cancelled": False}

    async def forever(_wait):
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    rig = make_rig(primary=[http_error(503), ok()], fallback=[ok()], sleep=forever)
    resp = run(post(rig))
    assert resp.status == 504 and resp.json["elapsed_ms"] == 2000
    assert resp.json["attempts"] == 1
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)
    assert state["cancelled"]
    assert "request.finished" in rig.names()


def test_cut_off_wait_dispatches_nothing_further_even_if_time_keeps_passing():
    async def forever(_wait):
        await asyncio.get_running_loop().create_future()

    rig = make_rig(primary=[http_error(503)], fallback=[ok()], sleep=forever)

    async def scenario():
        resp = await post(rig)
        await asyncio.sleep(30)                       # nothing may start later
        return resp

    resp = run(scenario())
    assert resp.status == 504
    assert (rig.primary.call_count, rig.fallback.call_count) == (1, 0)


def test_sleeper_error_is_a_defect_not_a_deadline():
    async def broken(_wait):
        raise TimeoutError("sleeper bug")

    rig = make_rig(primary=[http_error(503)], fallback=[ok()], sleep=broken)
    resp = run(post(rig))
    assert resp.status == 500 and resp.json["error"]["code"] == "internal_error"
    assert rig.fallback.call_count == 0


def test_normal_wait_is_unaffected_by_the_bound():
    rig = make_rig(primary=[http_error(503), ok()])
    body = run(post(rig)).json
    assert body["elapsed_ms"] == 100 and body["attempts"] == 2
    assert not rig.of("retry.wait_cut_off")


def test_real_clock_stalled_sleeper_returns_at_the_deadline():
    async def go():
        p = StubProvider("primary", "m", [hang()])
        f = StubProvider("fallback", "m2", [ok()])
        events = []

        async def sixty(_wait):
            await asyncio.sleep(60)

        gw = build_gateway(p, f, policy=Policy(total_deadline_s=0.5, per_call_timeout_s=0.2),
                           sink=events.append, sleep=sixty)
        t = time.monotonic()
        resp = await asyncio.wait_for(__import__("gateway.simclient", fromlist=["x"]).asgi_post(build_app(gw)), 5)
        return resp, time.monotonic() - t, p, f

    resp, wall, p, f = asyncio.run(go())
    assert resp.status == 504
    assert 0.45 <= wall < 1.5                        # not 60 s
    assert (p.call_count, f.call_count) == (1, 0)


# -- 2. sanitized logging ----------------------------------------------------------------

class LeakyProvider:
    id = "primary"
    model = "leaky-1"
    execution_mode = "stub"

    def __init__(self, exc_factory):
        self._factory = exc_factory

    async def call(self, text, ctx):
        raise self._factory(text)


def _rig_with(provider):
    events = []
    fb = StubProvider("fallback", "stub-fallback-1", [ok()])
    gw = build_gateway(provider, fb, sink=events.append)
    return build_app(gw), events


def _formatted(caplog):
    """Everything an operator log handler would print, including rendered tracebacks."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(fmt.format(r) for r in caplog.records)


def _leak_check(caplog, resp):
    out = _formatted(caplog)
    for forbidden in (PROMPT, CRED):
        assert forbidden not in out, f"{forbidden!r} leaked into logs:\n{out}"
        assert forbidden not in caplog.text
        assert forbidden not in resp.raw.decode()
    for r in caplog.records:
        assert forbidden_free(r)
    return out


def forbidden_free(record):
    blob = repr(record.__dict__) + record.getMessage()
    return PROMPT not in blob and CRED not in blob


def test_exception_text_with_prompt_and_credential_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    app, _ = _rig_with(LeakyProvider(lambda text: RuntimeError(f"upstream said {text!r} auth={CRED}")))
    from gateway.simclient import asgi_post
    resp = run(asgi_post(app, body_for(input=PROMPT)))
    assert resp.status == 500 and resp.json["error"]["code"] == "internal_error"
    out = _leak_check(caplog, resp)
    # The operator still gets a usable diagnostic: request id, exception type, frames.
    assert "request_id=" in out and "builtins.RuntimeError" in out and "test_round2_regressions.py" in out


def test_no_traceback_rendering_or_exc_info_is_attached(caplog):
    caplog.set_level(logging.DEBUG)
    app, _ = _rig_with(LeakyProvider(lambda text: ValueError(CRED)))
    from gateway.simclient import asgi_post
    run(asgi_post(app, body_for(input=PROMPT)))
    errors = [r for r in caplog.records if r.name.startswith("gateway") and r.levelno >= logging.ERROR]
    assert errors
    assert all(r.exc_info is None and r.exc_text is None for r in errors)
    assert "Traceback" not in _formatted(caplog)


def test_chained_exception_messages_are_not_logged(caplog):
    caplog.set_level(logging.DEBUG)

    def make(text):
        try:
            raise KeyError(CRED)
        except KeyError as inner:
            err = RuntimeError(f"wrapped {text}")
            err.__cause__ = inner
            return err

    app, _ = _rig_with(LeakyProvider(make))
    from gateway.simclient import asgi_post
    resp = run(asgi_post(app, body_for(input=PROMPT)))
    _leak_check(caplog, resp)


def test_exception_whose_str_and_repr_are_hostile_cannot_break_logging(caplog):
    caplog.set_level(logging.DEBUG)

    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError(CRED)

        __repr__ = __str__

    app, _ = _rig_with(LeakyProvider(lambda text: Hostile(PROMPT)))
    from gateway.simclient import asgi_post
    resp = run(asgi_post(app, body_for(input=PROMPT)))
    assert resp.status == 500
    _leak_check(caplog, resp)


def test_stub_crash_message_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    rig = make_rig(primary=[crash()])
    resp = run(post(rig, body_for(input=PROMPT)))
    assert resp.status == 500
    out = _formatted(caplog)
    assert "secret-internal-detail" not in out and PROMPT not in out


def test_unexpected_error_outside_the_engine_is_a_sanitized_500_too(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    import gateway.app as app_module

    def boom(content_type, raw):
        raise ValueError(f"parser blew up on {raw!r} {CRED}")

    monkeypatch.setattr(app_module, "parse_request", boom)
    rig = make_rig()
    resp = run(post(rig, body_for(input=PROMPT)))
    assert resp.status == 500
    body = resp.json
    assert body["error"] == {"code": "internal_error", "message": "Internal gateway error."}
    assert body["request_id"] is None and body["attempts"] == 0
    _leak_check(caplog, resp)
    assert rig.primary.call_count == 0
