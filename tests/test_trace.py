"""Contract section 5: trace events."""

import json

from gateway.providers import hang, http_error, malformed, ok
from helpers import REQUEST_ID, body_for, make_rig, post, run

REQUIRED = {"event", "request_id", "attempt", "provider", "model", "elapsed_ms", "outcome", "reason"}


def test_every_event_has_the_required_fields():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(post(rig))
    run(post(rig))
    assert rig.events
    for e in rig.events:
        assert REQUIRED <= set(e), e


def test_attempt_numbers_count_dispatched_calls_only_and_skips_are_null():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(post(rig))                       # P, P, F -> attempts 1,2,3 ; circuit opens
    run(post(rig))                       # P skipped, F -> attempt 1
    starts = [(e["request_id"], e["attempt"], e["provider"]) for e in rig.of("attempt.start")]
    assert [s[1] for s in starts] == [1, 2, 3, 1]
    skips = rig.of("provider.skipped")
    assert skips and all(e["attempt"] is None for e in skips)
    for e in rig.of("attempt.end"):
        assert isinstance(e["attempt"], int)


def test_routing_reasons_are_recorded():
    rig = make_rig(primary=[http_error(503)], fallback=[ok()])
    run(post(rig))
    assert [e["reason"] for e in rig.of("attempt.start")] == [
        "primary_initial", "primary_retry", "fallback_primary_failed"
    ]
    run(post(rig))
    assert rig.of("attempt.start")[-1]["reason"] == "fallback_primary_skipped"
    assert rig.of("provider.skipped")[-1]["reason"] == "circuit_open"


def test_elapsed_ms_is_non_decreasing_within_a_request():
    rig = make_rig(primary=[http_error(503), ok()])
    run(post(rig))
    xs = [e["elapsed_ms"] for e in rig.events]
    assert xs == sorted(xs) and xs[-1] == 100


def test_timeout_is_traced_as_uncertain_with_duplicate_work_warning():
    rig = make_rig(primary=[hang(), ok()])
    run(post(rig))
    t = [e for e in rig.of("attempt.end") if e["reason"] == "timeout"]
    assert len(t) == 1
    assert t[0]["remote_outcome"] == "uncertain"
    assert "uncertain" in t[0]["note"] and "duplicate" in t[0]["note"]
    known = [e for e in rig.of("attempt.end") if e["reason"] != "timeout"]
    assert all(e["remote_outcome"] == "known" for e in known)


def test_outcome_values_distinguish_qualifying_from_non_qualifying():
    for step, expect in (
        (http_error(503), "qualifying_failure"),
        (http_error(403), "non_qualifying_rejected"),
        (malformed(200, {}), "non_qualifying_malformed"),
        (ok(), "success"),
    ):
        rig = make_rig(primary=[step])
        run(post(rig))
        assert rig.of("attempt.end")[0]["outcome"] == expect


def test_request_finished_records_final_outcome_and_reason():
    rig = make_rig(primary=[http_error(403)])
    run(post(rig))
    fin = rig.of("request.finished")[0]
    assert (fin["outcome"], fin["status"], fin["attempts"]) == ("upstream_rejected", 502, 1)
    assert fin["reason"] == "non_retryable_upstream_outcome"


def test_validation_failures_are_traced_with_null_or_valid_request_id():
    rig = make_rig()
    run(post(rig, body_for(route="nope")))
    run(post(rig, body_for(request_id="bad")))
    ids = [e["request_id"] for e in rig.of("request.finished")]
    assert ids == [REQUEST_ID, None]
    assert all(e["outcome"] == "invalid_request" for e in rig.of("request.finished"))


def test_default_logs_exclude_prompts_outputs_credentials_and_upstream_bodies():
    secret_prompt = "PROMPT-SECRET-123"
    rig = make_rig(
        primary=[http_error(503, body={"error": "UPSTREAM-BODY-SECRET", "auth": "Bearer TOKEN-XYZ"})],
        fallback=[ok()],
    )
    resp = run(post(rig, body_for(input=secret_prompt),
                    headers={"content-type": "application/json", "authorization": "Bearer HEADER-SECRET"}))
    output = resp.json["output"]
    blob = json.dumps(rig.events)
    for forbidden in (secret_prompt, output, "UPSTREAM-BODY-SECRET", "TOKEN-XYZ", "HEADER-SECRET",
                      "Bearer", "authorization"):
        assert forbidden not in blob, forbidden
