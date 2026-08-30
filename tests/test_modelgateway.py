"""Model gateway (route A P3): task-token auth, upstream proxy (streaming),
usage ledger. Pure/unit — a local fake OpenAI-compatible upstream."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dswarm.solver.modelgateway import (
    GatewayUsageBridge,
    ModelGateway,
    TokenCapError,
    WorkerClaims,
    _Handler,
)


# ── fake upstream (OpenAI-compatible, records the Authorization header) ──────

class _FakeUpstream:
    def __init__(self):
        self.received_auth: list[str] = []
        self.error_status = None
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
                    self.send_response(upstream.error_status or 200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"}}]}\n\n")
                    self.wfile.write(b"data: {\"usage\":{\"input_tokens\":11,\"output_tokens\":7}}\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    out = json.dumps({"choices": [{"message": {"content": "OK"}}],
                                      "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()
                    self.send_response(upstream.error_status or 200)
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
    monkeypatch.setattr("dswarm.solver.modelgateway._UPSTREAM_BASE",
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


@pytest.mark.asyncio
async def test_gateway_usage_bridge_routes_each_run_to_its_registered_bus():
    from dswarm.core.events import Event, EventType

    seen_a = []
    seen_b = []

    class Bus:
        def __init__(self, seen):
            self.seen = seen

        async def emit_checked(self, event):
            self.seen.append(event)
            return event

    loop = asyncio.get_running_loop()
    bridge = GatewayUsageBridge(loop=loop, bus=Bus(seen_a), timeout=1.0)
    bridge.register("run-b", bus=Bus(seen_b), loop=loop)

    event_a = Event(event_type=EventType.USAGE_RECORDED, run_id="run-a", payload={})
    event_b = Event(event_type=EventType.USAGE_RECORDED, run_id="run-b", payload={})
    await asyncio.gather(
        asyncio.to_thread(bridge.publish, event_a),
        asyncio.to_thread(bridge.publish, event_b),
    )

    assert seen_a == [event_a]
    assert seen_b == [event_b]


@pytest.mark.asyncio
async def test_gateway_usage_bridge_publishes_on_owner_loop_and_waits():
    from dswarm.core.events import Event, EventType

    seen = []

    class Bus:
        async def emit_checked(self, event):
            seen.append((event, asyncio.get_running_loop()))
            await asyncio.sleep(0)
            return event

    loop = asyncio.get_running_loop()
    bridge = GatewayUsageBridge(loop=loop, bus=Bus(), timeout=1.0)
    event = Event(
        event_type=EventType.USAGE_RECORDED,
        run_id="bridge-run",
        payload={"usage_id": "usage::bridge-run::gateway::call"},
    )

    result = await asyncio.to_thread(bridge.publish, event)

    assert result is event
    assert seen == [(event, loop)]


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



def _claims(worker: str, *, run_id: str = "run-workers", scope: str = "worker") -> WorkerClaims:
    return WorkerClaims(
        run_id=run_id,
        challenge_id="challenge-1",
        worker_instance_id=worker,
        solver_id=f"solver-{worker}",
        profile_id="pi-web",
        configured_account_id="deepseek-primary",
        token_scope=scope,
    )


def test_per_worker_tokens_in_same_run_do_not_revoke_each_other() -> None:
    from concurrent.futures import ThreadPoolExecutor

    gw = ModelGateway(host="127.0.0.1", port=0)
    claims = [_claims(f"worker-{index}") for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(gw.issue_worker, claims))

    assert len(set(tokens)) == len(tokens)
    assert [gw.claims_for_token(token) for token in tokens] == claims
    assert all(gw.run_for_token(token) == "run-workers" for token in tokens)


def test_token_revoke_apis_are_scoped() -> None:
    gw = ModelGateway(host="127.0.0.1", port=0)
    token_a = gw.issue_worker(_claims("worker-a"))
    token_b = gw.issue_worker(_claims("worker-b"))
    token_other = gw.issue_worker(_claims("worker-other", run_id="run-other"))

    snapshot = gw.claims_for_token(token_a)
    gw.revoke_token(token_a)
    assert snapshot == _claims("worker-a")
    assert gw.claims_for_token(token_a) is None
    assert gw.claims_for_token(token_b) == _claims("worker-b")

    gw.revoke_worker("worker-b")
    assert gw.claims_for_token(token_b) is None
    assert gw.claims_for_token(token_other) is not None

    gw.revoke_run("run-other")
    assert gw.claims_for_token(token_other) is None


def test_token_hard_cap_rejects_without_evicting_active_tokens() -> None:
    gw = ModelGateway(host="127.0.0.1", port=0, max_active_tokens=2)
    first = gw.issue_worker(_claims("worker-1"))
    second = gw.issue_worker(_claims("worker-2"))

    with pytest.raises(TokenCapError) as caught:
        gw.issue_worker(_claims("worker-3"))

    assert caught.value.alert_payload == {
        "level": "error",
        "reason": "token_cap",
        "active_tokens": 2,
        "max_active_tokens": 2,
    }
    assert gw.claims_for_token(first) == _claims("worker-1")
    assert gw.claims_for_token(second) == _claims("worker-2")

class _RecordingUsageBridge:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _journal_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:] if line.strip()]


def test_gateway_journal_records_claims_and_canonical_event(upstream, tmp_path):
    _, gw = upstream
    gw.account_root = None
    bridge = _RecordingUsageBridge()
    gw.usage_bridge = bridge
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-gateway-journal"
    claims = _claims("journal-worker", run_id="run-journal")
    try:
        token = gw.issue_worker(claims)
        status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    assert status == 200
    journal = tmp_path / "run-journal-usage-journal.jsonl"
    rows = _journal_rows(journal)
    assert [row["phase"] for row in rows] == ["started", "finished"]
    started, finished = rows
    assert started["provider_call_id"]
    assert started["usage_id"] == finished["usage_id"]
    for field in ("run_id", "challenge_id", "worker_instance_id", "solver_id", "profile_id", "configured_account_id"):
        assert started[field] == getattr(claims, field)
        assert finished[field] == getattr(claims, field)
    assert finished["call_outcome"] == "succeeded"
    assert finished["usage_status"] == "measured"
    assert finished["input_tokens"] == 3
    assert finished["output_tokens"] == 2
    assert len(bridge.events) == 1
    assert bridge.events[0].event_type.value == "usage.recorded"
    legacy = tmp_path / "run-journal-gateway-usage.jsonl"
    assert legacy.exists()


def test_gateway_started_is_durable_before_upstream(upstream, tmp_path):
    up, gw = upstream
    gw.account_root = None
    journal = tmp_path / "run-order-usage-journal.jsonl"
    seen = []
    original = up._srv.RequestHandlerClass.do_POST

    def wrapped(handler):
        seen.append(journal.exists())
        return original(handler)

    up._srv.RequestHandlerClass.do_POST = wrapped
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-order"
    try:
        token = gw.issue("run-order")
        status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    assert status == 200
    assert seen == [True]


def test_gateway_started_failure_is_fail_closed(upstream, monkeypatch):
    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-fail-closed"
    from dswarm.core.usage_journal import AccountingUnavailable

    def fail_started(self, call):
        raise AccountingUnavailable("disk unavailable")

    monkeypatch.setattr("dswarm.solver.modelgateway.UsageJournal.append_started", fail_started)
    try:
        token = gw.issue("run-fail-closed")
        status, body = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    assert status == 503
    assert b"accounting_unavailable" in body
    assert up.received_auth == []


def test_gateway_provider_error_gets_terminal_unknown(upstream, tmp_path, monkeypatch):
    up, gw = upstream
    gw.account_root = None
    bridge = _RecordingUsageBridge()
    gw.usage_bridge = bridge
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-provider-error"
    up.error_status = 429
    try:
        token = gw.issue("run-provider-error")
        status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    assert status == 429
    rows = _journal_rows(tmp_path / "run-provider-error-usage-journal.jsonl")
    assert rows[-1]["call_outcome"] == "provider_error"
    assert rows[-1]["usage_status"] == "measured"
    assert rows[-1]["input_tokens"] == 3
    assert bridge.events[-1].payload["call_outcome"] == "provider_error"


def test_gateway_transport_error_gets_terminal_unknown(tmp_path, monkeypatch):
    gw = ModelGateway(host="127.0.0.1", port=0)
    gw.start()
    gw.sessions_root = str(tmp_path)
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-transport-error"
    monkeypatch.setattr("dswarm.solver.modelgateway._UPSTREAM_BASE", "http://127.0.0.1:1")
    try:
        token = gw.issue("run-transport-error")
        gw._test_port = gw._srv.server_address[1]  # type: ignore[attr-defined]
        status, body = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=False)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
        gw.stop()
    assert status == 502
    assert b"upstream error" in body
    rows = _journal_rows(tmp_path / "run-transport-error-usage-journal.jsonl")
    assert rows[-1]["call_outcome"] == "transport_error"
    assert rows[-1]["usage_status"] == "unknown"

@pytest.mark.asyncio

def test_gateway_call_keeps_entry_claims_after_token_revoke(upstream, tmp_path):
    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-entry-claims"
    claims = WorkerClaims(
        run_id="run-entry-claims",
        challenge_id="challenge-1",
        worker_instance_id="worker-entry",
        solver_id="solver-entry",
        profile_id="pi-web",
        configured_account_id="acct-1",
        token_scope="worker",
    )
    token = gw.issue_worker(claims)
    original = up._srv.RequestHandlerClass.do_POST
    revoked = []

    def wrapped(handler):
        gw.revoke_token(token)
        revoked.append(True)
        return original(handler)

    up._srv.RequestHandlerClass.do_POST = wrapped
    try:
        status, _ = _post(
            gw, token,
            {"model": "deepseek-v4-flash", "messages": []},
            stream=False,
        )
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)

    assert status == 200
    assert revoked == [True]
    assert gw.claims_for_token(token) is None
    rows = _journal_rows(tmp_path / "run-entry-claims-usage-journal.jsonl")
    assert rows[-1]["worker_instance_id"] == "worker-entry"
    assert rows[-1]["solver_id"] == "solver-entry"

async def test_gateway_usage_bridge_unregister_does_not_fallback_to_stale_default_bus():
    from dswarm.core.events import Event, EventType

    seen_default = []
    seen_run = []

    class Bus:
        def __init__(self, seen):
            self.seen = seen

        async def emit_checked(self, event):
            self.seen.append(event)
            return event

    loop = asyncio.get_running_loop()
    bridge = GatewayUsageBridge(loop=loop, bus=Bus(seen_default), timeout=0.2)
    bridge.register("run-a", bus=Bus(seen_run), loop=loop)
    bridge.unregister("run-a")

    event = Event(event_type=EventType.USAGE_RECORDED, run_id="run-a", payload={})
    with pytest.raises(RuntimeError, match="unavailable"):
        await asyncio.to_thread(bridge.publish, event)

    assert seen_default == []
    assert seen_run == []


def test_normalize_upstream_request_snaps_invalid_bigmodel_effort():
    """bigmodel 1210: always-thinking models reject reasoning_effort medium/
    minimal (only low/high/max). The gateway is the single upstream funnel, so
    it normalizes the dialect for bigmodel upstreams only."""
    from dswarm.solver.modelgateway import normalize_upstream_request

    bigmodel = "https://open.bigmodel.cn/api/paas/v4"
    req = normalize_upstream_request(
        {"model": "glm-5.3-flash", "reasoning_effort": "medium"}, upstream_base=bigmodel)
    assert req["reasoning_effort"] == "low"
    req = normalize_upstream_request(
        {"model": "glm-5.3-flash", "reasoning_effort": "minimal"}, upstream_base=bigmodel)
    assert req["reasoning_effort"] == "low"
    # valid values pass through untouched
    req = normalize_upstream_request(
        {"model": "glm-5.3-flash", "reasoning_effort": "high"}, upstream_base=bigmodel)
    assert req["reasoning_effort"] == "high"
    # absent effort stays absent
    req = normalize_upstream_request({"model": "glm-5.3-flash"}, upstream_base=bigmodel)
    assert "reasoning_effort" not in req
    # other upstreams are not rewritten
    req = normalize_upstream_request(
        {"model": "glm-5.3-flash", "reasoning_effort": "medium"},
        upstream_base="https://api.deepseek.com")
    assert req["reasoning_effort"] == "medium"


def test_gateway_streamed_usage_is_measured(upstream, tmp_path):
    """The streamed proxy must keep the raw SSE "data:" prefix on collected
    chunks: _extract_usage parses SSE lines, and stripping the prefix made
    every streamed call's usage unknown even though the provider sent the
    usage tail chunk."""
    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-stream-usage"
    try:
        token = gw.issue("run-stream-usage")
        status, _ = _post(gw, token, {"model": "deepseek-v4-flash", "messages": []}, stream=True)
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    assert status == 200
    rows = _journal_rows(tmp_path / "run-stream-usage-usage-journal.jsonl")
    assert rows[-1]["call_outcome"] == "succeeded"
    assert rows[-1]["usage_status"] == "measured"
    assert rows[-1]["input_tokens"] == 11
    assert rows[-1]["output_tokens"] == 7


def test_gateway_client_disconnect_after_success_keeps_single_succeeded_record(
        upstream, tmp_path):
    """A worker disconnecting after the stream completed must NOT append a
    second contradictory terminal record (conflicting usage id: the same call
    was finished 'succeeded' and then again 'transport_error')."""
    import http.client

    up, gw = upstream
    gw.account_root = None
    import os as _os
    _os.environ["DEEPSEEK_API_KEY"] = "sk-disconnect"
    try:
        token = gw.issue("run-disconnect")
        conn = http.client.HTTPConnection("127.0.0.1", gw._test_port, timeout=10)
        conn.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": "deepseek-v4-flash", "messages": [], "stream": True}),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        resp = conn.getresponse()
        resp.read()  # full successful stream
        # drop the connection abruptly, like a dying worker process
        conn.close()
        import time
        time.sleep(0.5)  # let the handler's write-back failure surface
    finally:
        _os.environ.pop("DEEPSEEK_API_KEY", None)
    rows = _journal_rows(tmp_path / "run-disconnect-usage-journal.jsonl")
    finished = [r for r in rows if r.get("phase") == "finished"]
    assert len(finished) == 1
    assert finished[0]["call_outcome"] == "succeeded"
