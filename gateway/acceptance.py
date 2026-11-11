"""Deterministic acceptance sequence R1..R7 plus independent cases (contract sections 4, 5).

Run:  python -m gateway.acceptance [--json report.json]

Timing basis: every latency in this report is SIMULATED (event-loop clock, see
gateway.simclock). The only measured wall-clock number is ``measured_wall_clock`` (how long
this process took to run the whole simulation). All providers are stubs.
All outcomes below are what this run OBSERVED; the "expected" columns are the contract's
predictions, checked by explicit assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
import time
from typing import Any, Callable

from .breaker import State
from .config import BreakerConfig, Policy
from .engine import Gateway
from .factory import build_app, build_gateway
from .providers import (
    CallContext,
    RawResponse,
    Step,
    StubProvider,
    hang,
    http_error,
    ok,
)
from .scenarios import CALL_MS, RID, ScheduledStubProvider, acceptance_scenario
from .simclient import asgi_post, request_body
from .simclock import run_simulated

POLICY = Policy()
BREAKER = BreakerConfig()

# (label, start_ms, primary schedule, fallback schedule, expected behaviour)
SEQUENCE = [
    ("R1", 0, "success", "success", dict(provider="primary", calls=1, elapsed_ms=10)),
    ("R2", 1000, "fail, then success", "success", dict(provider="primary", calls=2, elapsed_ms=120)),
    ("R3", 2000, "fail, fail", "success", dict(provider="fallback", calls=3, elapsed_ms=130)),
    ("R4", 3000, "fail if called", "success", dict(provider="fallback", calls=1, elapsed_ms=10)),
    ("R5", 8000, "fail", "success", dict(provider="fallback", calls=2, elapsed_ms=20)),
    ("R6", 9000, "fail if called", "success", dict(provider="fallback", calls=1, elapsed_ms=10)),
    ("R7", 14000, "success", "success", dict(provider="primary", calls=1, elapsed_ms=10)),
]
EXPECTED_TOTALS = dict(
    completed=7, provider_calls=11, primary_calls=7, fallback_calls=4, fallback_requests=4,
    primary_final_state="closed",
)
EXPECTED_BASELINE = dict(completed=2, provider_calls=7)


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, name: str, ok_: bool, expected: Any = None, observed: Any = None) -> bool:
        self.items.append(
            {"name": name, "passed": bool(ok_), "expected": expected, "observed": observed}
        )
        return bool(ok_)

    @property
    def all_passed(self) -> bool:
        return all(i["passed"] for i in self.items)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sim_sink(events: list[dict], t0_box: list[float]) -> Callable[[dict], None]:
    def sink(event: dict) -> None:
        loop = asyncio.get_running_loop()
        events.append({**event, "sim_t_ms": round((loop.time() - t0_box[0]) * 1000)})

    return sink


def _rig(
    primary: Any, fallback: Any, *, policy: Policy | None = None,
    breaker: BreakerConfig | None = None, sleep: Callable | None = None,
):
    events: list[dict] = []
    t0 = [asyncio.get_running_loop().time()]
    kwargs = {"sleep": sleep} if sleep else {}
    gw = build_gateway(
        primary, fallback, policy=policy, breaker=breaker, sink=_sim_sink(events, t0), **kwargs
    )
    return build_app(gw), gw, events


def _breaker(gw: Gateway, index: int):
    return gw.routes["standard"][index].breaker


def _calls(events: list[dict], request_id: str | None = None) -> list[dict]:
    return [
        e for e in events
        if e["event"] == "attempt.start" and (request_id is None or e["request_id"] == request_id)
    ]


def check_invariants(events: list[dict], policy: Policy, checks: Checks, label: str) -> None:
    """Call caps and no dispatch at or after the deadline, over a set of events."""
    by_req: dict[str, list[dict]] = {}
    for e in _calls(events):
        by_req.setdefault(e["request_id"], []).append(e)
    cap_ok = all(
        len(v) <= policy.max_calls
        and sum(1 for e in v if e["role"] == "primary") <= policy.primary_max_calls
        and sum(1 for e in v if e["role"] == "fallback") <= policy.fallback_max_calls
        for v in by_req.values()
    )
    checks(f"{label}: call caps (<=3 total, primary<=2, fallback<=1)", cap_ok)
    total_ms = policy.total_deadline_s * 1000
    late = [
        e for e in _calls(events)
        if e["elapsed_ms"] >= total_ms or e["elapsed_ms"] + e["timeout_ms"] > total_ms + 1
    ]
    checks(f"{label}: no dispatch at/after deadline, no call may outlive it", not late,
           observed=[(e["request_id"], e["elapsed_ms"], e["timeout_ms"]) for e in late])


# ---------------------------------------------------------------------------
# main sequence + baseline
# ---------------------------------------------------------------------------


async def _gateway_sequence(checks: Checks) -> dict:
    scenario = acceptance_scenario()
    primary = ScheduledStubProvider("primary", "stub-primary-1", scenario)
    fallback = ScheduledStubProvider("fallback", "stub-fallback-1", scenario)
    app, gw, events = _rig(primary, fallback)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    rows = []
    for label, start_ms, _p, _f, expect in SEQUENCE:
        await asyncio.sleep(max(0.0, t0 + start_ms / 1000 - loop.time()))
        started = loop.time()
        resp = await asgi_post(app, request_body(request_id=RID[label], input=f"prompt {label}"))
        body = resp.json
        rows.append(
            {
                "label": label,
                "request_id": RID[label],
                "start_ms": start_ms,
                "http_status": resp.status,
                "completed": resp.status == 200,
                "provider": body.get("provider"),
                "provider_calls": body["attempts"],
                "fallback_used": body.get("fallback_used", False),
                "elapsed_ms": body["elapsed_ms"],
                "elapsed_ms_loop_check": round((loop.time() - started) * 1000),
                "expected": expect,
            }
        )
        ok_ = (
            resp.status == 200
            and body["provider"] == expect["provider"]
            and body["attempts"] == expect["calls"]
            and body["elapsed_ms"] == expect["elapsed_ms"]
        )
        checks(f"{label}: provider/calls/elapsed match contract", ok_, expect,
               {k: rows[-1][v] for k, v in
                (("provider", "provider"), ("calls", "provider_calls"), ("elapsed_ms", "elapsed_ms"))})
    totals = dict(
        requests=len(rows),
        completed=sum(r["completed"] for r in rows),
        provider_calls=sum(r["provider_calls"] for r in rows),
        primary_calls=len(primary.calls),
        fallback_calls=len(fallback.calls),
        fallback_requests=sum(r["fallback_used"] for r in rows),
        primary_final_state=_breaker(gw, 0).state.value,
    )
    for k, want in EXPECTED_TOTALS.items():
        checks(f"gateway total {k}", totals[k] == want, want, totals[k])
    check_invariants(events, POLICY, checks, "sequence")
    return {"rows": rows, "totals": totals, "events": events, "primary": primary, "fallback": fallback}


async def _baseline(checks: Checks) -> dict:
    """Direct primary: one call per request, no retry, fallback or circuit."""
    scenario = acceptance_scenario()
    primary = ScheduledStubProvider("primary", "stub-primary-1", scenario)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    rows = []
    for label, start_ms, _p, _f, _e in SEQUENCE:
        await asyncio.sleep(max(0.0, t0 + start_ms / 1000 - loop.time()))
        started = loop.time()
        completed = False
        try:
            async with asyncio.timeout(POLICY.per_call_timeout_s):
                raw: RawResponse = await primary.call(
                    f"prompt {label}", CallContext(RID[label], 1, 1)
                )
            completed = (
                200 <= raw.status < 300
                and isinstance(raw.body, dict)
                and bool(raw.body.get("output"))
            )
        except TimeoutError:
            pass
        rows.append(
            {
                "label": label,
                "request_id": RID[label],
                "start_ms": start_ms,
                "completed": completed,
                "provider_calls": 1,
                "elapsed_ms": round((loop.time() - started) * 1000),
            }
        )
    totals = dict(
        requests=len(rows),
        completed=sum(r["completed"] for r in rows),
        provider_calls=len(primary.calls),
    )
    for k, want in EXPECTED_BASELINE.items():
        checks(f"baseline total {k}", totals[k] == want, want, totals[k])
    return {"rows": rows, "totals": totals}


# ---------------------------------------------------------------------------
# independent cases (fresh state each)
# ---------------------------------------------------------------------------

RID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _stubs(p: list[Step] | None, f: list[Step] | None):
    return StubProvider("primary", "stub-primary-1", p), StubProvider("fallback", "stub-fallback-1", f)


async def _case_both_fail(checks: Checks) -> dict:
    p, f = _stubs([http_error(503, delay_s=0.01)], [http_error(503, delay_s=0.01)])
    app, gw, ev = _rig(p, f)
    r = await asgi_post(app)
    b = r.json
    obs = dict(status=r.status, code=b["error"]["code"], calls=b["attempts"],
               primary_state=_breaker(gw, 0).state.value, fallback_state=_breaker(gw, 1).state.value,
               fallback_failures=_breaker(gw, 1).consecutive_failures)
    checks("both-fail: 503, 3 calls, P open, F closed with 1 failure",
           obs == dict(status=503, code="providers_unavailable", calls=3, primary_state="open",
                       fallback_state="closed", fallback_failures=1), observed=obs)
    check_invariants(ev, POLICY, checks, "both-fail")
    return {"name": "both_fail", "observed": obs}


async def _case_deadline(checks: Checks) -> dict:
    async def slow_sleep(_seconds: float) -> None:  # injected sleeper: delayed to the deadline
        await asyncio.sleep(1.5)

    p, f = _stubs([hang()], [ok()])
    app, gw, ev = _rig(p, f, sleep=slow_sleep)
    r = await asgi_post(app)
    b = r.json
    obs = dict(status=r.status, code=b["error"]["code"], calls=b["attempts"],
               primary_calls=p.call_count, fallback_calls=f.call_count, elapsed_ms=b["elapsed_ms"])
    checks("deadline: 504, one call, no dispatch after the delayed retry wait",
           obs == dict(status=504, code="deadline_exceeded", calls=1, primary_calls=1,
                       fallback_calls=0, elapsed_ms=2000), observed=obs)
    check_invariants(ev, POLICY, checks, "deadline")
    return {"name": "deadline", "observed": obs}


async def _case_invalid(checks: Checks) -> dict:
    # Leave one failure on the books so "unchanged circuits" is a real comparison.
    p, f = _stubs([http_error(429, retry_after="5", delay_s=0.01), ok()], [ok()])
    app, gw, ev = _rig(p, f)
    await asgi_post(app)
    snap = lambda: [(b.state.value, b.consecutive_failures, b.generation)  # noqa: E731
                    for b in (_breaker(gw, 0), _breaker(gw, 1))]
    before, calls_before = snap(), (p.call_count, f.call_count)
    bad = {
        "invalid route": request_body(route="fast"),
        "invalid input": request_body(input="   "),
        "extra field": request_body(model="gpt"),
    }
    obs = {}
    for name, body in bad.items():
        r = await asgi_post(app, body)
        obs[name] = r.status
    checks("invalid: 422 for bad route / input / extra field",
           all(v == 422 for v in obs.values()), observed=obs)
    checks("invalid: zero provider calls", (p.call_count, f.call_count) == calls_before)
    checks("invalid: circuits unchanged", snap() == before, before, snap())
    return {"name": "invalid", "observed": obs}


async def _case_non_retryable(checks: Checks) -> dict:
    obs = {}
    for status in (400, 401, 403):
        p, f = _stubs([http_error(status, delay_s=0.01)], [ok()])
        app, gw, ev = _rig(p, f)
        r = await asgi_post(app)
        waits = [e for e in ev if e["event"] in ("retry.wait", "retry.skipped")]
        obs[status] = dict(http=r.status, primary_calls=p.call_count, fallback_calls=f.call_count,
                           retry_events=len(waits), elapsed_ms=r.json["elapsed_ms"])
        checks(f"non-retryable {status}: 502, one call, no fallback, no retry wait",
               obs[status] == dict(http=502, primary_calls=1, fallback_calls=0, retry_events=0,
                                   elapsed_ms=10), observed=obs[status])
    return {"name": "non_retryable", "observed": obs}


async def _case_concurrent_recovery(checks: Checks) -> dict:
    p, f = _stubs([http_error(503, delay_s=0.01)] * 2 + [ok(delay_s=0.3)], [ok(delay_s=0.01)])
    app, gw, ev = _rig(p, f)
    await asgi_post(app)  # opens the circuit
    await asyncio.sleep(5.1)
    results = await asyncio.gather(*[asgi_post(app) for _ in range(5)])
    probes = [e for e in _calls(ev) if e["probe"]]
    providers = sorted(r.json["provider"] for r in results)
    obs = dict(probes=len(probes), providers=providers, primary_calls=p.call_count)
    checks("concurrent recovery: exactly one probe, the rest skip primary",
           obs == dict(probes=1, providers=["fallback"] * 4 + ["primary"], primary_calls=3),
           observed=obs)
    return {"name": "concurrent_recovery", "observed": obs}


async def _case_late_completion(checks: Checks) -> dict:
    p, f = _stubs([ok(delay_s=0.4), http_error(503, delay_s=0.01)], [ok(delay_s=0.01)])
    app, gw, ev = _rig(p, f)
    a = asyncio.create_task(asgi_post(app, request_body(request_id=RID_A)))
    await asyncio.sleep(0)
    b = await asgi_post(app, request_body(request_id=RID_B))
    a = await a
    obs = dict(a_status=a.status, a_provider=a.json["provider"], b_provider=b.json["provider"],
               primary_state=_breaker(gw, 0).state.value)
    checks("late completion: older success cannot close a circuit newer failures opened",
           obs == dict(a_status=200, a_provider="primary", b_provider="fallback",
                       primary_state="open"), observed=obs)
    return {"name": "late_completion", "observed": obs}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _latency(rows: list[dict]) -> dict:
    xs = [r["elapsed_ms"] for r in rows]
    return {"basis": "simulated ms, all requests incl. failures", "per_request_ms": xs,
            "mean_ms": round(statistics.mean(xs), 1), "median_ms": statistics.median(xs),
            "max_ms": max(xs)}


def _circuit_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if e["event"] == "breaker.transition":
            out.append({"sim_t_ms": e["sim_t_ms"], "request_id": e["request_id"],
                        "provider": e["provider"], "from": e["from"], "to": e["to"],
                        "reason": e["reason"],
                        **({"open_until_sim_ms": e["sim_t_ms"] + int(BREAKER.open_duration_s * 1000)}
                           if e["to"] == "open" else {})})
        elif e["event"] == "attempt.start" and e["probe"]:
            out.append({"sim_t_ms": e["sim_t_ms"], "request_id": e["request_id"],
                        "provider": e["provider"], "event": "probe_dispatched",
                        "attempt": e["attempt"]})
    return out


def build_report() -> tuple[dict, Checks]:
    checks = Checks()
    wall0 = time.perf_counter()

    async def everything() -> dict:
        seq = await _gateway_sequence(checks)
        return {"seq": seq}

    seq = run_simulated(everything())["seq"]
    base = run_simulated(_baseline(checks))
    cases = []
    for case in (_case_both_fail, _case_deadline, _case_invalid, _case_non_retryable,
                 _case_concurrent_recovery, _case_late_completion):
        cases.append(run_simulated(case(checks)))  # fresh loop + fresh state per case
    wall_ms = round((time.perf_counter() - wall0) * 1000, 1)

    report = {
        "report_version": 1,
        "labels": {
            "timing": "SIMULATED: event-loop clock, not wall-clock",
            "execution_mode": "stub (no real provider was called)",
            "expectations": "predictions from the contract; see assertions for observed vs expected",
        },
        "configuration": {
            "policy": dataclasses.asdict(POLICY),
            "breaker": dataclasses.asdict(BREAKER),
            "stub_call_ms": CALL_MS,
            "failure_status": 503,
            "scenario": "acceptance-R1-R7",
            "failure_keying": "(scenario, request_id, provider, provider-local attempt number)",
        },
        "failure_schedule": [
            {"request": lbl, "request_id": RID[lbl], "start_sim_ms": start,
             "primary": prim, "fallback": fb, "expected": exp}
            for lbl, start, prim, fb, exp in SEQUENCE
        ],
        "gateway": {
            "requests": seq["rows"],
            "totals": seq["totals"],
            "latency": _latency(seq["rows"]),
            "circuit_events": _circuit_events(seq["events"]),
            "trace_event_count": len(seq["events"]),
        },
        "baseline_direct_primary": {
            "requests": base["rows"],
            "totals": base["totals"],
            "latency": _latency(base["rows"]),
        },
        "comparison": {
            "completion_rate_gateway": seq["totals"]["completed"] / seq["totals"]["requests"],
            "completion_rate_baseline": base["totals"]["completed"] / base["totals"]["requests"],
            "calls_per_request_gateway": round(seq["totals"]["provider_calls"] / seq["totals"]["requests"], 3),
            "calls_per_request_baseline": round(base["totals"]["provider_calls"] / base["totals"]["requests"], 3),
        },
        "additional_cases": cases,
        "assertions": checks.items,
        "all_assertions_passed": checks.all_passed,
        "measured_wall_clock": {
            "run_ms": wall_ms,
            "note": "Real time this process took to execute the whole simulation. "
                    "Not a latency of any request.",
        },
        "remote_outcome_note": "A timeout leaves the provider outcome uncertain; a retry or "
                               "fallback after one can duplicate work or billing. No "
                               "exactly-once claim is made.",
    }
    return report, checks


def render_terminal(report: dict) -> str:
    g, b, c = report["gateway"], report["baseline_direct_primary"], report["comparison"]
    out = [
        "ACCEPTANCE REPORT  [timings SIMULATED | providers STUB | wall-clock shown separately]",
        "",
        f"{'req':<4} {'start':>6}  {'http':>4} {'served by':<9} {'calls':>5} {'elapsed':>8}  "
        f"{'expected':<26} baseline",
    ]
    for r, br in zip(g["requests"], b["requests"]):
        e = r["expected"]
        out.append(
            f"{r['label']:<4} {r['start_ms']:>6}  {r['http_status']:>4} {r['provider']:<9} "
            f"{r['provider_calls']:>5} {r['elapsed_ms']:>6}ms  "
            f"{e['provider'] + '/' + str(e['calls']) + '/' + str(e['elapsed_ms']) + 'ms':<26} "
            f"{'ok' if br['completed'] else 'FAILED'}"
        )
    t, bt = g["totals"], b["totals"]
    out += [
        "",
        f"gateway : {t['completed']}/{t['requests']} completed, {t['provider_calls']} calls "
        f"({t['primary_calls']} primary + {t['fallback_calls']} fallback), "
        f"{t['fallback_requests']} requests used fallback, primary ends {t['primary_final_state']}",
        f"baseline: {bt['completed']}/{bt['requests']} completed, {bt['provider_calls']} calls",
        f"calls/request: gateway {c['calls_per_request_gateway']}  baseline "
        f"{c['calls_per_request_baseline']}",
        f"latency (simulated ms, all requests): gateway mean {g['latency']['mean_ms']} / "
        f"max {g['latency']['max_ms']}; baseline mean {b['latency']['mean_ms']}",
        "",
        "circuit events (simulated ms):",
    ]
    for ev in g["circuit_events"]:
        if "event" in ev:
            out.append(f"  {ev['sim_t_ms']:>6}  {ev['provider']:<8} probe dispatched ({ev['request_id'][-2:]})")
        else:
            extra = f"  (cooldown until {ev['open_until_sim_ms']})" if "open_until_sim_ms" in ev else ""
            out.append(f"  {ev['sim_t_ms']:>6}  {ev['provider']:<8} {ev['from']} -> {ev['to']}  [{ev['reason']}]{extra}")
    out += ["", "additional cases (fresh state each):"]
    for case in report["additional_cases"]:
        out.append(f"  {case['name']}: {json.dumps(case['observed'], sort_keys=True)}")
    fails = [a for a in report["assertions"] if not a["passed"]]
    out += [
        "",
        f"assertions: {len(report['assertions']) - len(fails)}/{len(report['assertions'])} passed",
    ]
    for a in fails:
        out.append(f"  FAIL {a['name']}: expected {a['expected']} observed {a['observed']}")
    out.append(f"measured wall-clock for the whole simulation: {report['measured_wall_clock']['run_ms']} ms")
    out.append("note: a timeout leaves the remote outcome uncertain; retries/fallbacks may duplicate work or billing.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gateway.acceptance")
    ap.add_argument("--json", help="write the JSON report to this path")
    args = ap.parse_args(argv)
    report, checks = build_report()
    print(render_terminal(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"\nJSON report written to {args.json}")
    return 0 if checks.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
