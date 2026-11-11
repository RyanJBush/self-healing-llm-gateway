"""Contract section 4: the deterministic R1..R7 sequence, baseline, and the report."""

import json
import subprocess
import sys
from pathlib import Path

from gateway.acceptance import build_report
from gateway.providers import CallContext
from gateway.scenarios import RID, ScheduledStubProvider, acceptance_scenario
from helpers import run

ROOT = Path(__file__).resolve().parent.parent


def test_every_acceptance_assertion_passes():
    report, checks = build_report()
    failed = [a for a in checks.items if not a["passed"]]
    assert not failed, failed
    assert report["all_assertions_passed"]


def test_gateway_sequence_matches_contract_row_by_row():
    report, _ = build_report()
    rows = {r["label"]: r for r in report["gateway"]["requests"]}
    got = {k: (v["provider"], v["provider_calls"], v["elapsed_ms"]) for k, v in rows.items()}
    assert got == {
        "R1": ("primary", 1, 10),
        "R2": ("primary", 2, 120),
        "R3": ("fallback", 3, 130),
        "R4": ("fallback", 1, 10),
        "R5": ("fallback", 2, 20),
        "R6": ("fallback", 1, 10),
        "R7": ("primary", 1, 10),
    }
    assert all(r["http_status"] == 200 for r in rows.values())
    t = report["gateway"]["totals"]
    assert (t["completed"], t["provider_calls"], t["primary_calls"], t["fallback_calls"],
            t["fallback_requests"], t["primary_final_state"]) == (7, 11, 7, 4, 4, "closed")


def test_circuit_timeline_matches_contract():
    report, _ = build_report()
    transitions = [
        (e["sim_t_ms"], e["from"], e["to"])
        for e in report["gateway"]["circuit_events"] if "to" in e
    ]
    assert transitions == [
        (2120, "closed", "open"),        # R3: two failures, opens at 2,120
        (8000, "open", "half_open"),     # R5: cooldown over, probe admitted
        (8010, "half_open", "open"),     # probe fails; reopens until 13,010
        (14000, "open", "half_open"),    # R7
        (14010, "half_open", "closed"),  # probe succeeds
    ]
    opens = [e for e in report["gateway"]["circuit_events"] if e.get("to") == "open"]
    assert [e["open_until_sim_ms"] for e in opens] == [7120, 13010]


def test_baseline_matches_contract():
    report, _ = build_report()
    t = report["baseline_direct_primary"]["totals"]
    assert (t["completed"], t["provider_calls"]) == (2, 7)
    done = [r["label"] for r in report["baseline_direct_primary"]["requests"] if r["completed"]]
    assert done == ["R1", "R7"]


def test_report_separates_simulated_stub_and_wall_clock():
    report, _ = build_report()
    assert "SIMULATED" in report["labels"]["timing"]
    assert "stub" in report["labels"]["execution_mode"]
    assert report["measured_wall_clock"]["run_ms"] >= 0
    assert "uncertain" in report["remote_outcome_note"]
    assert "exactly-once claim" in report["remote_outcome_note"]
    json.dumps(report)  # fully serialisable


def test_report_contains_required_sections():
    report, _ = build_report()
    for key in ("configuration", "failure_schedule", "gateway", "baseline_direct_primary",
                "comparison", "additional_cases", "assertions", "measured_wall_clock"):
        assert key in report
    assert {"requests", "totals", "latency", "circuit_events"} <= set(report["gateway"])
    names = " ".join(a["name"] for a in report["assertions"])
    assert "call caps" in names and "no dispatch" in names


def test_cli_writes_json_and_prints_summary(tmp_path):
    out = tmp_path / "report.json"
    r = subprocess.run(
        [sys.executable, "-m", "gateway.acceptance", "--json", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SIMULATED" in r.stdout and "assertions:" in r.stdout
    assert json.loads(out.read_text())["all_assertions_passed"] is True


def test_failure_schedule_is_keyed_not_a_shared_queue():
    """Same answers no matter what order or how many times other requests call."""

    async def play(order):
        p = ScheduledStubProvider("primary", "m", acceptance_scenario())
        out = {}
        for label, attempt in order:
            raw = await p.call("x", CallContext(RID[label], attempt, attempt))
            out[(label, attempt)] = raw.status
        return out

    expected = {("R2", 1): 503, ("R2", 2): 200, ("R3", 1): 503, ("R3", 2): 503,
                ("R1", 1): 200, ("R5", 1): 503, ("R7", 1): 200}
    forward = list(expected)
    for order in (forward, forward[::-1], [forward[3], forward[0], forward[5], forward[1],
                                           forward[6], forward[2], forward[4]]):
        assert run(play(order)) == expected
    # Extra, unrelated calls (as the baseline vs gateway would make) change nothing.
    noisy = [("R4", 1), ("R6", 1)] + forward
    assert {k: v for k, v in run(play(noisy)).items() if k in expected} == expected


def test_scheduled_stub_calls_take_exactly_10_simulated_ms():
    async def go():
        import asyncio
        p = ScheduledStubProvider("primary", "m", acceptance_scenario())
        loop = asyncio.get_running_loop()
        t = loop.time()
        await p.call("x", CallContext(RID["R1"], 1, 1))      # success
        a = loop.time() - t
        t = loop.time()
        await p.call("x", CallContext(RID["R2"], 1, 1))      # 503
        return a, loop.time() - t

    ok_s, fail_s = run(go())
    assert round(ok_s * 1000) == 10 and round(fail_s * 1000) == 10
