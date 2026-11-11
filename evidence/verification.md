# Verification record

This publication copy preserves the reviewed MVP engine, provider, validation, breaker,
and application code. Publication changes add documentation, licensing, and evidence.

## Local publication checks

- `python -m pytest -q`: 380 passing tests. [Exact output](test-output.txt).
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
