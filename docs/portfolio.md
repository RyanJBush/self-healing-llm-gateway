# Self-Healing LLM Gateway

A local gateway that keeps a small text-generation request within an explicit attempt
budget, retries temporary failures, and switches to an approved fallback. Every routing
and circuit decision appears in a sanitized trace.

## Problem

A temporary provider failure can cause an application to fail even when a fallback is
available. Unbounded retries can create long waits or duplicate provider work. This project
makes those decisions explicit and testable.

## Implementation

Python, ASGI, HTTPX, uvicorn, and pytest. The gateway validates a fixed request contract,
allows at most three calls across the configured providers, and applies a two-second
request deadline including retry waits. Each provider has its own circuit breaker with
a five-second cooldown and one recovery probe at a time. The initial implementation was
written with Claude; Codex independently reviewed it and reproduced the critical checks.

## Evidence

In the same seven-request deterministic failure schedule, the gateway completed **7/7**
requests using **11 provider calls**; a direct-primary baseline completed **2/7** using
**seven calls**. Four requests used fallback. The primary circuit recovered as expected.
These are stub results with simulated request timings, not real-provider reliability,
latency, or cost claims.

The reviewed suite contained **382 passing tests**, including real local HTTP timeout and
client-disconnect checks. Independent review found and verified fixes for an unbounded
retry sleeper and exception messages leaking into logs.

[Repository](https://github.com/RyanJBush/self-healing-llm-gateway) ·
[Reproduce the demo](../README.md#verify) ·
[Observed report](../evidence/acceptance-report.txt) ·
[Verification](../evidence/verification.md) ·
[Architecture](architecture.md)

## Scope

This is a local, non-streaming portfolio MVP using two deterministic provider stubs.
It does not claim real LLM-provider validation or production readiness. Circuit state is
process-local; a timeout does not prove remote work stopped. Real-provider integration,
macOS, and load beyond five simultaneous requests remain unverified. CI results are
recorded in the verification page.
