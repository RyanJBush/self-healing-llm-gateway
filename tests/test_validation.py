"""Contract section 1: request validation and the error envelope."""

import json

import pytest

from helpers import REQUEST_ID, body_for, make_rig, post, run

ERROR_KEYS = {"request_id", "error", "attempts", "elapsed_ms"}


def check_422(resp, *, request_id=None):
    assert resp.status == 422
    body = resp.json
    assert set(body) == ERROR_KEYS
    assert body["error"]["code"] == "invalid_request"
    assert set(body["error"]) == {"code", "message"}
    assert body["request_id"] == request_id
    assert body["attempts"] == 0
    assert isinstance(body["elapsed_ms"], int)
    return body


def rejected(body=None, **kw):
    rig = make_rig()
    resp = run(post(rig, body, **kw))
    # Zero provider calls on any validation failure.
    assert rig.primary.call_count == 0 and rig.fallback.call_count == 0
    return resp


def test_valid_request_succeeds_and_echoes_request_id():
    rig = make_rig()
    resp = run(post(rig))
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/json"
    body = resp.json
    assert set(body) == {
        "request_id", "output", "provider", "model", "execution_mode",
        "attempts", "fallback_used", "elapsed_ms",
    }
    assert body["request_id"] == REQUEST_ID
    assert body["output"] and isinstance(body["output"], str)
    assert (body["provider"], body["model"]) == ("primary", "stub-primary-1")
    assert body["execution_mode"] == "stub"
    assert (body["attempts"], body["fallback_used"]) == (1, False)
    assert isinstance(body["elapsed_ms"], int)


@pytest.mark.parametrize("text", ["a", "x" * 4000, "  padded  ", "héllo 🌍", "line1\nline2"])
def test_input_boundaries_accepted(text):
    rig = make_rig()
    assert run(post(rig, body_for(input=text))).status == 200


def test_input_counts_code_points_not_bytes():
    # 4000 four-byte characters: valid by the contract (characters, not bytes).
    rig = make_rig()
    assert run(post(rig, body_for(input="🌍" * 4000))).status == 200
    assert run(post(rig, body_for(input="🌍" * 4001))).status == 422


@pytest.mark.parametrize(
    "bad_input",
    ["", " ", "   \t\n ", "x" * 4001, None, 5, 1.5, True, ["a"], {"a": 1}, "bad\ud800"],
)
def test_bad_input_rejected_and_request_id_echoed(bad_input):
    resp = rejected(json.dumps(
        {"request_id": REQUEST_ID, "input": bad_input, "route": "standard"}
    ).encode())
    check_422(resp, request_id=REQUEST_ID)


@pytest.mark.parametrize("route", ["", "fast", "Standard", "STANDARD", None, 1, ["standard"]])
def test_bad_route_rejected(route):
    check_422(rejected(body_for(route=route)), request_id=REQUEST_ID)


@pytest.mark.parametrize(
    "rid",
    [
        "", "abc", "123E4567-E89B-42D3-A456-426614174000",   # uppercase
        "123e4567e89b42d3a456426614174000",                    # no hyphens
        "{123e4567-e89b-42d3-a456-426614174000}",              # braces
        "urn:uuid:123e4567-e89b-42d3-a456-426614174000",
        " 123e4567-e89b-42d3-a456-426614174000",
        "123e4567-e89b-42d3-a456-426614174000\n",
        "123e4567-e89b-42d3-a456-42661417400",                 # short
        "123e4567-e89b-42d3-a456-4266141740000",               # long
        "g23e4567-e89b-42d3-a456-426614174000",                # non-hex
        None, 5, True, ["x"],
    ],
)
def test_bad_request_id_rejected_with_null_request_id(rid):
    check_422(rejected(body_for(request_id=rid)), request_id=None)


def test_unsafe_request_id_is_never_echoed():
    marker = "<script>alert(1)</script>"
    resp = rejected(body_for(request_id=marker))
    assert marker not in resp.raw.decode()
    assert resp.json["request_id"] is None


@pytest.mark.parametrize("missing", ["request_id", "input", "route"])
def test_missing_field_rejected(missing):
    data = {"request_id": REQUEST_ID, "input": "hi", "route": "standard"}
    del data[missing]
    resp = rejected(json.dumps(data).encode())
    check_422(resp, request_id=None if missing == "request_id" else REQUEST_ID)


@pytest.mark.parametrize(
    "extra",
    ["provider", "model", "timeout_ms", "base_url", "scenario", "fail", "temperature", "x"],
)
def test_unknown_fields_rejected_so_clients_cannot_steer_providers(extra):
    data = {"request_id": REQUEST_ID, "input": "hi", "route": "standard", extra: "primary"}
    resp = rejected(json.dumps(data).encode())
    body = check_422(resp, request_id=REQUEST_ID)
    assert body["error"]["message"] == "Request body contains unknown fields."


@pytest.mark.parametrize(
    "raw",
    [
        b"", b"{", b"not json", b"[]", b"null", b"42", b'"str"', b"true",
        b'{"request_id": "%s", "input": "a", "input": "b", "route": "standard"}' % REQUEST_ID.encode(),
        b'{"request_id": "%s", "input": NaN, "route": "standard"}' % REQUEST_ID.encode(),
        b"\xff\xfe\x00bad utf8",
        b"\xef\xbb\xbf{}",
        b"[" * 100000,
    ],
)
def test_malformed_json_rejected(raw):
    check_422(rejected(raw))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"content-type": "text/plain"},
        {"content-type": "application/x-www-form-urlencoded"},
        {"content-type": "application/json; charset=latin-1"},
        {"content-type": "application/jsonx"},
    ],
)
def test_wrong_content_type_rejected(headers):
    check_422(rejected(body_for(), headers=headers), request_id=None)


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_accepted_content_types(content_type):
    rig = make_rig()
    assert run(post(rig, headers={"content-type": content_type})).status == 200


def test_oversized_body_rejected_without_provider_calls():
    resp = rejected(b" " * (64 * 1024 + 1))
    check_422(resp)


def test_error_messages_are_fixed_text_and_do_not_reflect_input():
    secret = "SECRET-INPUT-" + "x" * 4000
    resp = rejected(body_for(input=secret))  # too long
    assert "SECRET-INPUT" not in resp.raw.decode()


def test_unknown_path_and_method_use_the_envelope():
    rig = make_rig()
    r = run(post(rig, path="/nope"))
    assert r.status == 404 and r.json["error"]["code"] == "not_found"
    r = run(post(rig, method="GET"))
    assert r.status == 405 and r.json["error"]["code"] == "method_not_allowed"
    assert r.headers["allow"] == "POST"
    assert rig.primary.call_count == 0
