"""Model gateway (route A P3): task-token auth, upstream proxy (streaming),
usage ledger. Pure/unit — a local fake OpenAI-compatible upstream."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from muteki.solver.modelgateway import ModelGateway, _Handler


# ── fake upstream (OpenAI-compatible, records the Authorization header) ──────

class _FakeUpstream:
    def __init__(self):
        self.received_auth: list[str] = []
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _make_handler(self):
        upstream = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                upstream.received_auth.append(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = json.loads(body or b"{}")
                stream = bool(req.get("stream", False))
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"}}]}\n\n")
                    self.wfile.write(b"data: {\"usage\":{\"input_tokens\":11,\"output_tokens\":7}}\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    out = json.dumps({"choices": [{"message": {"content": "OK"}}],
                                      "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

        return H

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture()
def upstream(monkeypatch, tmp_path):
    up = _FakeUpstream()
    monkeypatch.setattr("muteki.solver.modelgateway._UPSTREAM_BASE",
                        f"http://127.0.0.1:{up.port}")
    # isolated gateway instance per test
    gw = ModelGateway(host="127.0.0.1", port=0)
    gw.start()
    gw.sessions_root = str(tmp_path)
    # expose the live port
    gw._test_port = gw._srv.server_address[1]  # type: ignore[attr-defined]
    yield up, gw
    gw.stop()
    up.close()


def _post(gw, token: str, body: dict, stream: bool = True) -> "tuple[int, bytes]":
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", gw._test_port, timeout=10)
    payload = json.dumps({**body, "stream": stream})
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", "/v1/chat/completions", body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_issue_revoke_and_auth(upstream):
    _, gw = upstream
    token = gw.issue("run-x")
    assert token and len(token) >= 32
    assert gw.run_for_token(token) == "run-x"
    assert gw.run_for_token("bogus") is None

    # re-issue revokes the old token (a re-run must not inherit credentials)
    token2 = gw.issue("run-x")
    assert gw.run_for_token(token) is None
    assert gw.run_for_token(token2) == "run-x"

    gw.revoke("run-x")
    assert gw.run_for_token(token2) is None


def test_missing_or_bad_token_is_401(upstream):
    _, gw = upstream
    status, body = _post(gw, "", {"model": "deepseek-v4-flash", "messages": []})
    assert status == 401
    assert b"invalid or revoked" in body

    gw.issue("run-y")
    status, _ = _post(gw, "deadbeef", {"model": "deepseek-v4-flash", "messages": []})
    assert status == 401

    token = gw.issue("run-z")
    gw.revoke("run-z")
    status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []})
    assert status == 401


def test_streaming_proxy_forwards_and_swaps_key(upstream):
    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-real-upstream-key"
    try:
        token = gw.issue("run-s")
        status, data = _post(gw, token,
                             {"model": "deepseek-v4-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                             stream=True)
        assert status == 200
        assert b"OK" in data                      # streamed content passed through
        assert b"[DONE]" in data
        assert up.received_auth == ["Bearer sk-real-upstream-key"]  # REAL key at upstream
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)


def test_non_streaming_proxy(upstream):
    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-non-stream"
    try:
        token = gw.issue("run-ns")
        status, data = _post(gw, token,
                             {"model": "deepseek-v4-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                             stream=False)
        assert status == 200
        assert b"OK" in data
        assert up.received_auth == ["Bearer sk-non-stream"]
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)


def test_usage_recorded_per_run(upstream, tmp_path):
    _, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-usage"
    try:
        token = gw.issue("run-u")
        _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=True)
        _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    # usage ledgers land under sessions_root
    ledger = tmp_path / "run-u-gateway-usage.jsonl"
    assert ledger.exists()
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows, "gateway must record usage"
    assert all(r["run_id"] == "run-u" for r in rows)
    assert rows[-1]["usage"].get("input_tokens") is not None


def test_account_root_key_wins_over_env(upstream, tmp_path):
    up, gw = upstream
    acct = tmp_path / "accounts" / "pi-main"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("sk-from-account\n", encoding="utf-8")
    gw.account_root = str(tmp_path / "accounts")
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    try:
        token = gw.issue("run-a")
        status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=True)
        assert status == 200
        assert up.received_auth == ["Bearer sk-from-account"]
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)


def test_health_endpoint(upstream):
    import http.client
    _, gw = upstream
    conn = http.client.HTTPConnection("127.0.0.1", gw._test_port, timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    assert b"ok" in resp.read()
    conn.close()
