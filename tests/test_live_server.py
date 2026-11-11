"""Real uvicorn process over real sockets (real clock, ~2 s total)."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
RID = "123e4567-e89b-42d3-a456-426614174000"
BODY = json.dumps({"request_id": RID, "input": "hi", "route": "standard"}).encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def server(tmp_path, scenario=None):
    env = dict(os.environ)
    if scenario is not None:
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(scenario))
        env["GATEWAY_STUB_SCENARIO"] = str(path)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def events_from(proc):
    out = proc.stdout.read()
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_success_and_validation_over_http(tmp_path):
    with server(tmp_path) as (port, _):
        url = f"http://127.0.0.1:{port}/v1/generations"
        r = httpx.post(url, content=BODY, headers={"content-type": "application/json"})
        assert r.status_code == 200 and r.json()["request_id"] == RID
        assert r.json()["execution_mode"] == "stub"
        bad = httpx.post(url, json={"request_id": RID, "input": "", "route": "standard"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
        assert httpx.get(url).status_code == 405


def test_retry_then_success_over_http(tmp_path):
    scenario = {"primary": [{"kind": "http", "status": 503}, {"kind": "ok"}]}
    with server(tmp_path, scenario) as (port, _):
        t = time.monotonic()
        r = httpx.post(f"http://127.0.0.1:{port}/v1/generations", content=BODY,
                       headers={"content-type": "application/json"})
        took = time.monotonic() - t
    body = r.json()
    assert (r.status_code, body["attempts"], body["provider"]) == (200, 2, "primary")
    assert 100 <= body["elapsed_ms"] < 400 and took >= 0.1


def test_client_disconnect_cancels_the_upstream_call(tmp_path):
    scenario = {"primary": [{"kind": "hang"}], "fallback": [{"kind": "ok"}]}
    with server(tmp_path, scenario) as (port, proc):
        sock = socket.create_connection(("127.0.0.1", port))
        head = (
            f"POST /v1/generations HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(BODY)}\r\n\r\n"
        ).encode()
        sock.sendall(head + BODY)
        time.sleep(0.25)       # primary call is now in flight
        sock.close()           # client goes away
        time.sleep(1.0)        # longer than the 500 ms timeout + 100 ms retry wait
    events = events_from(proc)
    names = [e["event"] for e in events]
    assert names.count("attempt.start") == 1, names     # no retry, no fallback
    assert "request.cancelled" in names and "request.finished" not in names


def test_refuses_to_bind_a_public_interface():
    r = subprocess.run(
        [sys.executable, "-m", "gateway", "--host", "0.0.0.0", "--port", str(free_port())],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 2 and "non-loopback" in r.stderr


def test_server_process_output_never_contains_exception_text(tmp_path):
    """Real process: the crash message and the prompt must not appear on stdout/stderr."""
    scenario = {"primary": [{"kind": "crash"}]}      # stub raises "...secret-internal-detail"
    prompt = "LIVE-PROMPT-MUST-NOT-APPEAR"
    body = json.dumps({"request_id": RID, "input": prompt, "route": "standard"})
    with server(tmp_path, scenario) as (port, proc):
        r = httpx.post(f"http://127.0.0.1:{port}/v1/generations", content=body,
                       headers={"content-type": "application/json"})
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        assert "secret-internal-detail" not in r.text and prompt not in r.text
    out = proc.stdout.read()
    assert "secret-internal-detail" not in out and prompt not in out
    assert "Traceback" not in out
    assert "unexpected gateway exception" in out       # but the operator still gets a line
