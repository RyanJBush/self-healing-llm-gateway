"""Hand-written mutation check: break the source one way at a time, expect the suite to fail.

Run from the project root:  python scripts/mutations.py [--json out.json] [--full]

Each mutant is applied to a throwaway copy. A mutant is KILLED if pytest fails, SURVIVED if
the suite still passes. By default the two slow real-clock files are skipped (--full keeps
them); every mutant here is expected to be caught by the deterministic simulated tests.
A survivor is either a test gap or an EQUIVALENT mutant (no observable behaviour change);
the "note" field says which and why.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (id, file, [(old, new, nth_occurrence)], what it breaks)
MUTANTS = [
    # --- retry / classification (engine.py) ---
    ("4xx-all-retryable", "gateway/engine.py",
     [("if status in (408, 429) or 500 <= status <= 599:",
       "if status in (408, 429) or 400 <= status <= 599:", 1)],
     "every 4xx becomes retryable (the reviewer's concern)"),
    ("only-429-retryable", "gateway/engine.py",
     [("if status in (408, 429) or", "if status in (429,) or", 1)],
     "408 stops being retryable"),
    ("5xx-not-retryable", "gateway/engine.py",
     [("or 500 <= status <= 599:\n            retry_after", "or 500 <= status <= 502:\n            retry_after", 1)],
     "only 500-502 retryable"),
    ("malformed-retryable", "gateway/engine.py",
     [('return CallResult(Kind.MALFORMED, "malformed_success_body")',
       'return CallResult(Kind.RETRYABLE, "malformed_success_body")', 1)],
     "malformed success is retried / charged to the circuit"),
    ("timeout-not-qualifying", "gateway/engine.py",
     [('return CallResult(Kind.RETRYABLE, "timeout", uncertain=True)',
       'return CallResult(Kind.REJECTED, "timeout", uncertain=True)', 1)],
     "timeouts stop being retryable"),
    ("non-qualifying-charged-to-circuit", "gateway/engine.py",
     [("elif result.kind is Kind.RETRYABLE:\n                        slot.breaker.record_failure",
       "elif result.kind is not Kind.SUCCESS:\n                        slot.breaker.record_failure", 1)],
     "4xx / malformed count toward opening the circuit"),
    # --- budget (engine.py) ---
    ("ignore-retry-after", "gateway/engine.py",
     [("wait = max(p.retry_wait_s, result.retry_after_s or 0.0)", "wait = p.retry_wait_s", 1)],
     "Retry-After ignored"),
    ("retry-after-boundary", "gateway/engine.py",
     [("if wait >= remaining:", "if wait > remaining:", 1)],
     "a wait that lands exactly on the deadline is allowed"),
    ("no-deadline-recheck", "gateway/engine.py",
     [("if now >= deadline - DEADLINE_EPSILON_S:  # recheck before every dispatch", "if False:", 1)],
     "deadline not rechecked before dispatch"),
    ("dispatch-at-deadline", "gateway/engine.py",
     [("if now >= deadline - DEADLINE_EPSILON_S:  # recheck before every dispatch",
       "if now > deadline:", 1)],
     "dispatch allowed exactly at the deadline"),
    ("timeout-ignores-remaining", "gateway/engine.py",
     [("timeout = min(p.per_call_timeout_s, deadline - now)", "timeout = p.per_call_timeout_s", 1)],
     "per-call timeout not clipped to remaining budget"),
    ("final-deadline-check-removed", "gateway/engine.py",
     [("        if loop.time() >= deadline - DEADLINE_EPSILON_S:\n            return self._error(rid, \"deadline_exceeded\", start, st.attempts)\n        return self._error(rid, \"providers_unavailable\"",
       "        return self._error(rid, \"providers_unavailable\"", 1)],
     "503 instead of 504 when the deadline expired on the last call"),
    # --- round-2 fixes ---
    ("retry-sleeper-unbounded", "gateway/engine.py",
     [("bound = asyncio.timeout_at(deadline)", "bound = asyncio.timeout_at(deadline + 1000)", 1)],
     "retry wait no longer cut off at the deadline"),
    ("exception-text-logged", "gateway/engine.py",
     [('log.error("unexpected gateway exception request_id=%s %s", req.request_id, describe(exc))',
       'log.exception("unexpected gateway exception (request_id=%s)", req.request_id)', 1)],
     "log.exception prints the exception message and traceback again"),
    ("handler-guard-removed", "gateway/app.py",
     [("            await self._guarded_http(scope, receive, send)", "            await self._http(scope, receive, send)", 1)],
     "no last-resort sanitized 500 outside the engine"),
    # --- caps / routing (engine.py) ---
    ("primary-3-calls", "gateway/engine.py",
     [("max_here = p.primary_max_calls if idx == 0 else p.fallback_max_calls",
       "max_here = 3 if idx == 0 else p.fallback_max_calls", 1)],
     "primary may be called three times"),
    ("fallback-retry-allowed", "gateway/engine.py",
     [("max_here = p.primary_max_calls if idx == 0 else p.fallback_max_calls",
       "max_here = p.primary_max_calls", 1),
      ("if idx != 0 or ticket.probe or calls_here >= max_here:",
       "if ticket.probe or calls_here >= max_here:", 1)],
     "fallback is retried (both redundant guards removed together)"),
    ("probe-may-retry", "gateway/engine.py",
     [("if idx != 0 or ticket.probe or calls_here >= max_here:",
       "if idx != 0 or calls_here >= max_here:", 1)],
     "a half-open probe gets a same-provider retry"),
    ("skip-consumes-attempt", "gateway/engine.py",
     [("                if ticket is None:\n                    if idx == 0:",
       "                if ticket is None:\n                    st.attempts += 1\n                    if idx == 0:", 1)],
     "a skipped provider uses up an attempt"),
    ("fallback_used-always-true", "gateway/engine.py",
     [("                if idx > 0:\n                    st.fallback_used = True",
       "                if idx >= 0:\n                    st.fallback_used = True", 1)],
     "fallback_used reported for primary-only requests"),
    # --- breaker ---
    ("threshold-3", "gateway/config.py",
     [("failure_threshold: int = 2", "failure_threshold: int = 3", 1)],
     "circuit needs three failures (the earlier assumption)"),
    ("cooldown-4s", "gateway/config.py",
     [("open_duration_s: float = 5.0", "open_duration_s: float = 4.0", 1)],
     "cooldown 4000 ms"),
    ("probe-release-stays-half-open", "gateway/breaker.py",
     [('self._move(State.OPEN, "probe_not_conclusive", events)', "pass", 1)],
     "non-qualifying / cancelled probe leaves the circuit half-open"),
    ("probe-release-keeps-slot", "gateway/breaker.py",
     [("                self._probe_in_flight = False\n                self._opened_at = now\n                self._move(State.OPEN, \"probe_not_conclusive\"",
       "                self._opened_at = now\n                self._move(State.OPEN, \"probe_not_conclusive\"", 1)],
     "probe slot leaks on release"),
    ("probe-failure-keeps-old-cooldown", "gateway/breaker.py",
     [("                    self._opened_at = now\n                    self._move(State.OPEN, \"probe_failed\", events)",
       "                    self._move(State.OPEN, \"probe_failed\", events)", 1)],
     "failed probe does not restart the cooldown"),
    ("two-probes-allowed", "gateway/breaker.py",
     [("            elif not self._probe_in_flight:  # HALF_OPEN with a free slot",
       "            elif True:  # HALF_OPEN with a free slot", 1)],
     "more than one probe in flight"),
    ("stale-success-honoured", "gateway/breaker.py",
     [("if self._settle(ticket) and ticket.generation == self.generation:\n                if ticket.probe:\n                    self._probe_in_flight = False\n                    self.consecutive_failures = 0",
       "if self._settle(ticket):\n                if ticket.probe:\n                    self._probe_in_flight = False\n                    self.consecutive_failures = 0", 1)],
     "stale success from an older generation is applied"),
    ("stale-failure-honoured", "gateway/breaker.py",
     [("if self._settle(ticket) and ticket.generation == self.generation:\n                if ticket.probe:\n                    self._probe_in_flight = False\n                    self._opened_at = now\n                    self._move(State.OPEN, \"probe_failed\"",
       "if self._settle(ticket):\n                if ticket.probe:\n                    self._probe_in_flight = False\n                    self._opened_at = now\n                    self._move(State.OPEN, \"probe_failed\"", 1)],
     "stale failure from an older generation is applied"),
    ("success-does-not-reset", "gateway/breaker.py",
     [("                else:\n                    self.consecutive_failures = 0\n        self._flush(events, ctx)\n\n    def record_failure",
       "                else:\n                    pass\n        self._flush(events, ctx)\n\n    def record_failure", 1)],
     "a success no longer resets the failure count"),
    ("failure-count-reset-by-release", "gateway/breaker.py",
     [("        events: list[dict] = []\n        with self._lock:\n            if self._settle(ticket) and ticket.probe and ticket.generation == self.generation:",
       "        events: list[dict] = []\n        with self._lock:\n            if not ticket.probe:\n                self.consecutive_failures = 0\n            if self._settle(ticket) and ticket.probe and ticket.generation == self.generation:", 1)],
     "a non-qualifying outcome resets the count"),
    # --- app / validation ---
    ("body-read-unbounded", "gateway/app.py",
     [("async with asyncio.timeout_at(deadline):", "async with asyncio.timeout_at(deadline + 1000):", 1)],
     "slow body not bounded by the deadline"),
    ("disconnect-ignored", "gateway/app.py",
     [('            if msg["type"] == "http.disconnect":\n                return\n\n    async def _read_body',
       '            if msg["type"] == "http.disconnect":\n                await asyncio.sleep(10**6)\n\n    async def _read_body', 1)],
     "client disconnect no longer cancels the work"),
    ("unknown-fields-allowed", "gateway/validation.py",
     [("if keys - REQUIRED_FIELDS:", "if False:", 1)], "extra fields accepted"),
    ("whitespace-input-allowed", "gateway/validation.py",
     [('if value.strip() == "":', "if False:", 1)], "whitespace-only input accepted"),
    ("uppercase-uuid-allowed", "gateway/validation.py",
     [('_UUID_RE = re.compile(\n    r"[0-9a-f]{8}', '_UUID_RE = re.compile(\n    r"(?i)[0-9a-f]{8}', 1)],
     "non-canonical uppercase UUID accepted"),
    ("duplicate-keys-allowed", "gateway/validation.py",
     [('raise ValueError("duplicate key")', "pass", 1)], "duplicate JSON keys accepted"),
]

# Mutants that change no observable behaviour. They are only "killed" by a white-box
# assertion on a private field, so they are NOT evidence of a behavioural protection.
EQUIVALENT: dict[str, str] = {
    "probe-release-keeps-slot": (
        "Behaviourally equivalent: every path into half-open re-sets the probe flag, so a "
        "stale flag in the open state is never observable. Detected only by "
        "`assert b._probe_in_flight is False` in test_breaker.py (private field)."
    ),
}

# Mutants whose protection is real but redundant or conditional; stated so the kill count
# is not read as 33 independent protections.
CAVEATS: dict[str, str] = {
    "fallback-retry-allowed": (
        "Two redundant guards (call cap and `idx != 0`) back each other up; the mutant "
        "removes both. Removing either alone is equivalent."
    ),
    "probe-may-retry": (
        "With the default 5 s cooldown the breaker already refuses a second probe, so the "
        "engine's own no-retry-after-probe rule is only observable with a zero cooldown "
        "(test_probe_gets_no_retry_even_if_the_breaker_would_admit_another_probe)."
    ),
}


def apply_edits(path: Path, edits) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new, nth in edits:
        positions = [m.start() for m in re.finditer(re.escape(old), text)]
        if len(positions) < nth:
            raise RuntimeError(f"pattern not found ({nth}th) in {path.name}: {old[:60]!r}")
        i = positions[nth - 1]
        text = text[:i] + new + text[i + len(old):]
    path.write_text(text, encoding="utf-8")


def run_suite(workdir: Path, full: bool) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-x", "--tb=no", "-rf", "-p", "no:cacheprovider"]
    if not full:
        cmd += ["--ignore=tests/test_live_server.py", "--ignore=tests/test_http_integration.py"]
    r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=600)
    first = next((l for l in r.stdout.splitlines() if l.startswith("FAILED")), "")
    return r.returncode, first.replace("FAILED ", "")[:140]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--full", action="store_true", help="include the slow real-clock tests")
    ap.add_argument("--only", help="comma-separated mutant ids")
    args = ap.parse_args()

    wanted = set(args.only.split(",")) if args.only else None
    results = []
    with tempfile.TemporaryDirectory(prefix="mut-") as tmp:
        base = Path(tmp)

        def fresh(name: str) -> Path:
            d = base / name
            shutil.copytree(ROOT / "gateway", d / "gateway", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "tests", d / "tests", ignore=shutil.ignore_patterns("__pycache__"))
            for f in ("pyproject.toml", "openapi.yaml"):
                shutil.copy(ROOT / f, d / f)
            return d

        code, _ = run_suite(fresh("baseline"), args.full)
        print(f"baseline (unmutated): {'PASS' if code == 0 else 'FAIL'}")
        if code != 0:
            return 2
        for mid, rel, edits, what in MUTANTS:
            if wanted and mid not in wanted:
                continue
            d = fresh(mid)
            apply_edits(d / rel, edits)
            code, first = run_suite(d, args.full)
            status = "KILLED" if code != 0 else "SURVIVED"
            if status == "KILLED" and mid in EQUIVALENT:
                detection = "white-box only (behaviourally equivalent)"
            elif status == "KILLED":
                detection = "behavioural"
            else:
                detection = "none"
            results.append({"id": mid, "file": rel, "change": what, "status": status,
                            "detection": detection, "first_failing_test": first,
                            "note": EQUIVALENT.get(mid) or CAVEATS.get(mid, "")})
            tag = "KILLED*" if detection.startswith("white-box") else status
            print(f"{tag:<8} {mid:<36} {first or ''}")
            shutil.rmtree(d, ignore_errors=True)

    killed = sum(r["status"] == "KILLED" for r in results)
    white = sum(r["detection"].startswith("white-box") for r in results)
    distinct = len({r["first_failing_test"].split(" - ")[0] for r in results if r["status"] == "KILLED"})
    print(f"\n{killed}/{len(results)} mutants killed: {killed - white} by behavioural assertions, "
          f"{white} only by a white-box assertion on a private field (behaviourally equivalent, "
          f"marked *)")
    print(f"{distinct} distinct first-failing tests (several mutants are caught by the same "
          f"aggregate test, so this is not {len(results)} independent protections)")
    survivors = [r["id"] for r in results if r["status"] == "SURVIVED"]
    if survivors:
        print("survivors:", ", ".join(survivors))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"scope": "full suite" if args.full else "simulated-clock suite (slow real-clock tests skipped)",
             "killed": killed, "total": len(results),
             "killed_by_behavioural_assertion": killed - white,
             "killed_only_by_white_box_assertion": white,
             "distinct_first_failing_tests": distinct,
             "reading": "Kill count is not 33 independent protections: several mutants share "
                        "one aggregate test, one mutant is behaviourally equivalent, and two "
                        "protections are redundant or config-conditional (see notes).",
             "results": results}, indent=2))
    return 0 if not survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
