# Decisions, assumptions, and reconciliation log

Contract status: the full contract (sections 1-6 plus the reconciliation handoff) has now
been received. Section numbers below refer to it. "v1" is the first implementation, which
was built from the truncated text; the original snapshots are retained in the private frozen review archive.

## A. Reconciliation: v1 assumptions checked against the full contract

| v1 assumption | Full contract says | Result |
|---|---|---|
| Failed fallback ends the request: 503, or 504 if the deadline expired | "A failed fallback ends the request even if the global attempt cap has unused capacity"; handoff confirms 503 unless the deadline expired, then 504 | **Held.** Now also tested with spare capacity (`test_failed_fallback_ends_the_request_even_with_unused_attempt_capacity`) |
| Circuit opens after **3** consecutive failures | **2** | **Changed.** `BreakerConfig.failure_threshold = 2` |
| Cooldown 5 s | 5,000 ms from the failure that opened it | **Held.** Boundary tested at exactly 5,000 ms |
| One probe in flight at a time | "single probe slot", acquired atomically at cooldown expiry (open -> half-open) | **Held** |
| Probe cancelled / non-qualifying: slot released, circuit **stays half-open** | Release the slot and **return to open with a fresh 5,000 ms cooldown**; do not claim recovery | **Changed.** `CircuitBreaker.release(ticket, now)` re-opens and restarts the cooldown |
| Stale completions ignored if the circuit moved | Ignore completions from calls admitted before a newer open/probe **generation** | **Strengthened.** Explicit generation counter; stale success *and* stale failure ignored |
| (not specified) lock | "Use a lock for state and probe admission, held only during state operations" | **Added.** `threading.Lock` in `CircuitBreaker`; events emitted after release |
| Canonical lowercase hyphenated UUID; integer-seconds or HTTP-date `Retry-After` | Accepted as clarifications | **Held** |
| HTTP-date uses wall clock | Keep wall-clock parsing separate from the monotonic deadline; make both controllable in tests | **Held and tested.** `wall_clock=` is only read to parse a `Retry-After` date |
| Error-message assertion for non-retryable 4xx | Not sufficient; assert 502, exactly one call, zero fallback calls, no retry wait | **Fixed.** `test_every_4xx_except_408_and_429_is_502_one_call_no_fallback_no_wait` (98 codes) |
| Unmeasured: "Inject a clock and sleeper" | Required for the deterministic sequence | **Added.** See D1 |

Also new from sections 4-6: scenario-keyed stubs, the R1-R7 sequence and direct-primary
baseline, the additional independent cases, the trace schema, the terminal + JSON report,
the OpenAPI document, and a real-HTTP provider for the local timeout integration test.

## B. Values taken from the contract

| Item | Value |
|---|---|
| Total deadline | 2,000 ms from handler entry (before body read and validation) |
| Per-call timeout | `min(500 ms, remaining)` |
| Max calls | 3 (primary <= 2, fallback <= 1) |
| Primary retry wait | 100 ms, raised to a valid `Retry-After` if larger |
| Circuit opens | 2 consecutive qualifying failures; 5,000 ms cooldown from the opening failure |
| Qualifying failure | connection failure, timeout, 408, 429, 5xx |
| Route `standard` | `primary` then `fallback`, from trusted config only |

## C. Interpretations (not contradicted by the full contract)

1. **Canonical UUID** = lowercase 8-4-4-4-12 hex. Echoed in an error envelope only if it
   validated; otherwise `null`.
2. **Input length** counts code points. Whitespace-only is `str.strip() == ""`. Text that
   cannot be encoded as UTF-8 (lone surrogate) is rejected.
3. **422 messages** are fixed text naming only known fields. They never reflect client text.
4. **Extra 422 triggers**: wrong/missing `Content-Type`, invalid UTF-8, duplicate JSON
   keys, non-object JSON, NaN/Infinity, body over 64 KiB.
5. **Other paths/methods**: same envelope shape, codes `not_found` (404) and
   `method_not_allowed` (405, `Allow: POST`). Outside the contract table; documented in
   `openapi.yaml` as `OutOfContractError`.
6. **Valid `Retry-After`** = ASCII-digit integer seconds, or an IMF-fixdate
   (`Fri, 15 Jan 2027 08:00:01 GMT`). Obsolete RFC 850 / asctime forms are invalid.
   A valid value applies to any qualifying failure carrying it; wait = `max(100 ms, requested)`.
   Per request only; not remembered.
7. **"Delay cannot fit"** = `wait >= remaining`. The retry is skipped and the fallback is
   considered immediately.
8. **2xx handling**: a JSON-object body with a non-empty string `output` is success. Any
   other 2xx body (including 204) is a malformed successful response: 502, no retry, no
   fallback, no circuit effect. 1xx/3xx are treated the same.
9. **Final status when everything fails**: 504 if the deadline is exhausted at decision
   time, else 503. All providers skipped = 503 with `attempts: 0`.
10. **Disconnect**: cancel the in-flight call, make no later call, write no response,
    emit `attempt.end` outcome `cancelled` with `remote_outcome: uncertain`. Not a circuit
    failure; a cancelled probe re-opens the circuit with a fresh cooldown (contract section 3).
11. **Unexpected exception** (including any non-contract exception from a provider) -> 500,
    no retry, no circuit failure, fixed message.
12. **`elapsed_ms`** = `round((end - start) * 1000)`, integer.
13. **Deadline margin**: anything within 1 microsecond of the deadline counts as "at the
    deadline". Only ever stricter. (Timers can fire a rounding hair early; on Windows the
    loop's clock resolution is coarser still, so real-clock tests keep wide margins.)
14. **Body read** is bounded by the deadline: 504 with `attempts: 0` and `request_id: null`.
    If the deadline passed by the time the body is read, 504 wins over 422.
15. **Stub output** is `stub:<provider_id>:<12 hex of sha256(input)>`; it does not echo input.
16. **Timeout uncertainty**: a timeout is traced `remote_outcome: uncertain` with a note
    that a retry/fallback can duplicate work or billing. `request_id` is documented as
    correlation only; the API never claims exactly-once execution.

## D. Implementation choices (not contract changes)

1. **Clocks.** Monotonic time = the running event loop's clock (`loop.time()`). The
   "injected clock and sleeper" for deterministic runs is `gateway.simclock.SimulatedLoop`
   (time advances only when the loop would sleep, so `asyncio.sleep`, `asyncio.timeout` and
   `loop.time()` agree) plus an explicit `sleep=` parameter on `Gateway` for tests that
   need the retry wait to overshoot. Wall-clock time is a separate `wall_clock=` parameter.
   There is no `clock=` parameter on purpose: per-call timeouts are `asyncio.timeout`,
   which reads the loop clock, so a second injectable clock could disagree with it.
2. **Stack deviates from the "suggested" FastAPI**: pure ASGI + uvicorn, so no framework
   validation payload can ever reach a client. HTTPX is used (the HTTP provider and tests),
   pytest for tests. `openapi.yaml` is hand-written; `tests/test_openapi.py` validates real
   responses and request acceptance against it.
3. **Providers** are behind `Provider.call(text, ctx)` returning a raw
   `(status, headers, body)`; the engine classifies it. `CallContext` carries the request
   ID (correlation header only, never an idempotency key) and the provider-local attempt
   number, which is how scenario stubs key failures.
4. **Failure schedule** is keyed `(scenario, request_id, provider, provider-local attempt)`;
   no shared outcome queue (`gateway/scenarios.py`).
5. **HttpProvider** (`execution_mode: real`) disables httpx transport retries and sets no
   httpx timeout; the gateway's `asyncio.timeout` cancels the call, which closes the
   connection. The only stubbed execution mode in the shipped app is `stub`.
6. Pure ASGI, one process, one worker, `127.0.0.1` only; the launcher refuses other hosts.
7. Events are one JSON object per line on logger `gateway.events`. They never contain
   input, output, credentials, authorization headers, or upstream bodies (tested).

## E. Known limits (stated, not hidden)

- Circuit state is process-local and resets on restart; it is not coordinated across workers.
- A closed client connection or a gateway-side timeout does not prove the remote work
  stopped. `tests/test_http_integration.py` asserts only what the local server can observe.
- The real-clock tests use wide margins (tens of ms) because Windows timers are coarse.
- `probe-release-keeps-slot` is behaviourally equivalent (the next open -> half-open
  transition re-takes the slot); it is killed only by a white-box assertion. See
  [mutation report](evidence/mutation-report.json).

## F. Round 2 (independent review) corrections

| Finding | Fix | Pinned by |
|---|---|---|
| The retry sleeper's `await` was not bounded: a 60 s sleeper gave 504 with `elapsed_ms = 60000` | The wait runs inside `asyncio.timeout_at(deadline)`; at the deadline the sleeper is cancelled, `retry.wait_cut_off` is traced, and the response is 504 at ~2,000 ms with nothing further dispatched. A `TimeoutError` raised by the sleeper itself is still a defect (500), not a deadline | `tests/test_round2_regressions.py` (sleeper 60 s, never-completing sleeper, post-cutoff silence, sleeper defect, unaffected normal wait, real-clock variant) |
| `log.exception` printed exception text (prompt, credential) | Unexpected-exception logs now carry request ID, exception type and call-site frames only (`gateway/safelog.py`); no `exc_info`, no `str(exc)`, no chained messages | Regressions capture and format every log record, including traceback rendering, plus a live-process test of real stdout/stderr |
| (same class of leak, outside the engine) | `GatewayApp._guarded_http` turns any unexpected exception in the handler into a sanitized 500 with the same sanitized log line, instead of the server's default traceback print | `test_unexpected_error_outside_the_engine_is_a_sanitized_500_too` |
| `test_openapi.py` imports PyYAML and jsonschema | Declared in the `test` extra with httpx and pytest; `[build-system]` and `packages = ["gateway"]` added so a clean `pip install ".[test]"` works | Clean-environment installs in [verification record](evidence/verification.md) |
| Mutation result overstated | `scripts/mutations.py` now labels the equivalent mutant `KILLED*`, adds caveats, prints distinct first-failing tests, and the README says plainly that the kill count (33 at the time, 36 after the round-2 mutants) is not that many independent protections | [mutation report](evidence/mutation-report.json) |

Added at the reviewer's request: `.github/workflows/ci.yml`, `docs/architecture.md`
(Mermaid, rendered and checked), and README sections for the verified comparison, failure
policy and limitations. No new product features.

## G. Publication CI timing correction (v0.2.2)

Windows/Python 3.11 CI exposed a retry after 94 ms rather than the configured minimum
100 ms. Keep the live assertion. The engine rechecks an absolute monotonic not-before
target after every sleep, retaining the total-deadline timeout around all waits.
Two deterministic regression cases cover the default delay and upstream Retry-After.
See [verification](evidence/verification.md#publication-timing-correction-v022).
