# Verification record

This publication starts from the reviewed MVP and adds a minimum-retry-delay fix found
by Windows/Python 3.11 CI, plus documentation, licensing, and public evidence.

## Local publication checks

- `python -m pytest -q`: 382 passing tests. [Exact output](test-output.txt).
- `python -m gateway.acceptance --json evidence/acceptance-report.json`: 31/31 assertions.
  [Text report](acceptance-report.txt) · [JSON report](acceptance-report.json).
- `python scripts/mutations.py --json evidence/mutation-report.json`: see the
  [mutation report](mutation-report.txt). Equivalent and shared-test caveats are retained.

The preview image in the README is a rendering of the captured terminal report, not a
recording of real-provider traffic. Request timings are simulated; providers are stubs.

## Independent review

The initial review found two contract violations: a retry sleeper could exceed the total
deadline, and unexpected exception messages could leak prompt text or credentials into
logs. Both were corrected and checked independently with regressions. The reviewed local
suite passed 380 tests; a separate clean Python 3.13 installation passed the same suite and
all acceptance assertions using the installed package.

## GitHub CI

Pending the first published workflow run. The workflow tests Windows and Ubuntu on
Python 3.11, 3.12, and 3.13, then runs the mutation checks.

## Still outside verified scope

Real LLM providers, macOS, load beyond five simultaneous requests, distributed circuit
state, streaming, and production deployment. Local HTTP tests verify cancellation and
timeouts against a controlled server; they do not establish remote LLM behavior or cost.

## Publication timing correction (v0.2.2)

The [first CI run](https://github.com/RyanJBush/self-healing-llm-gateway/actions/runs/42944253974)
passed five matrix jobs but failed Windows/Python 3.11: the observed retry elapsed time
was 94 ms rather than the configured 100 ms minimum. The assertion was retained.

Event-loop timers can wake up to one clock-resolution early
([Python documentation](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.call_later)).
The gateway now rechecks the monotonic minimum-wait target and waits the remaining time,
inside the same absolute deadline. Two deterministic regressions first reproduced retries
at 94 ms and 994 ms (instead of 100 ms and a one-second Retry-After), then passed with
the fix. The frozen pre-publication archive is retained separately and unchanged.
