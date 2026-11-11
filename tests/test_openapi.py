"""openapi.yaml is the contract; real responses must conform to it."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from gateway.providers import crash, hang, http_error, malformed, ok
from helpers import REQUEST_ID, body_for, make_rig, post, run

SPEC = yaml.safe_load((Path(__file__).resolve().parent.parent / "openapi.yaml").read_text())
RESPONSES = SPEC["paths"]["/v1/generations"]["post"]["responses"]


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}",
                                 "components": SPEC["components"]})


def schema_for_status(status: int) -> str:
    ref = RESPONSES[str(status)]["content"]["application/json"]["schema"]["$ref"]
    return ref.rsplit("/", 1)[1]


def check(resp):
    validator(schema_for_status(resp.status)).validate(resp.json)


def test_spec_documents_exactly_the_contract_statuses_and_codes():
    assert set(RESPONSES) == {"200", "422", "500", "502", "503", "504"}
    codes = SPEC["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"]["properties"]["code"]["enum"]
    assert set(codes) == {"invalid_request", "upstream_rejected", "providers_unavailable",
                          "deadline_exceeded", "internal_error"}
    assert "No exactly-once guarantee" in SPEC["info"]["description"]


@pytest.mark.parametrize(
    "name,build,status",
    [
        ("success", lambda: make_rig(), 200),
        ("retry-success", lambda: make_rig(primary=[http_error(503), ok()]), 200),
        ("fallback-success", lambda: make_rig(primary=[http_error(503)], fallback=[ok()]), 200),
        ("upstream-4xx", lambda: make_rig(primary=[http_error(403)]), 502),
        ("malformed", lambda: make_rig(primary=[malformed(200, {})]), 502),
        ("all-fail", lambda: make_rig(primary=[http_error(503)], fallback=[http_error(500)]), 503),
        ("all-hang", lambda: make_rig(primary=[hang()], fallback=[hang()]), 503),
        ("defect", lambda: make_rig(primary=[crash()]), 500),
    ],
)
def test_real_responses_conform_to_the_spec(name, build, status):
    resp = run(post(build()))
    assert resp.status == status
    check(resp)


def test_deadline_and_validation_responses_conform():
    rig = make_rig(primary=[hang()])
    r = run(post(rig, body_delay=1.9))
    assert r.status == 504
    check(r)
    r = run(post(make_rig(), body_delay=2.5))
    assert r.status == 504
    check(r)
    for bad in (body_for(route="x"), body_for(input=""), b"{", body_for(request_id="no"),
                json.dumps({"request_id": REQUEST_ID}).encode()):
        r = run(post(make_rig(), bad))
        assert r.status == 422
        check(r)


def test_out_of_contract_errors_conform_to_their_own_schema():
    rig = make_rig()
    for resp in (run(post(rig, path="/x")), run(post(rig, method="GET"))):
        validator("OutOfContractError").validate(resp.json)


VALID_REQUESTS = [
    {"request_id": REQUEST_ID, "input": "a", "route": "standard"},
    {"request_id": REQUEST_ID, "input": "x" * 4000, "route": "standard"},
    {"request_id": REQUEST_ID, "input": " ok ", "route": "standard"},
]
INVALID_REQUESTS = [
    {"request_id": REQUEST_ID, "input": "", "route": "standard"},
    {"request_id": REQUEST_ID, "input": "   ", "route": "standard"},
    {"request_id": REQUEST_ID, "input": "x" * 4001, "route": "standard"},
    {"request_id": REQUEST_ID, "input": "a", "route": "fast"},
    {"request_id": REQUEST_ID.upper(), "input": "a", "route": "standard"},
    {"request_id": REQUEST_ID, "input": "a", "route": "standard", "model": "m"},
    {"request_id": REQUEST_ID, "input": "a"},
    {"input": "a", "route": "standard"},
]


@pytest.mark.parametrize("req", VALID_REQUESTS)
def test_schema_and_implementation_agree_on_valid_requests(req):
    validator("GenerationRequest").validate(req)
    assert run(post(make_rig(), json.dumps(req).encode())).status == 200


@pytest.mark.parametrize("req", INVALID_REQUESTS)
def test_schema_and_implementation_agree_on_invalid_requests(req):
    assert not validator("GenerationRequest").is_valid(req)
    assert run(post(make_rig(), json.dumps(req).encode())).status == 422
