"""Web backend e2e (Sprint 1.1 acceptance): mock solver -> SSE -> assert the
frontend receives the full typed event stream; HITL POST lands in the run.

Runs a REAL uvicorn server on an ephemeral port in a background thread (the SSE
stream needs a real ASGI server — httpx.ASGITransport does not stream
incrementally), then drives it with httpx.
"""

import asyncio
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from dswarm.core.events import EventType
from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Server:
    def __init__(self, app) -> None:
        self.port = _free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        # wait for startup
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def server():
    app = create_app(RunManager(sessions_root="/tmp/dswarm_web_sessions"))
    with _Server(app) as s:
        yield s


async def _collect_sse(client: httpx.AsyncClient, run_id: str, seen: set,
                       stop_on: str) -> None:
    async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        cur = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
                seen.add(cur)
                if cur == stop_on:
                    return


async def _collect_sse_payload(client: httpx.AsyncClient, run_id: str,
                               stop_on: str) -> dict:
    async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        cur = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
            elif cur == stop_on and line.startswith("data:"):
                return json.loads(line.split(":", 1)[1].strip())["payload"]
    raise AssertionError(f"did not see {stop_on}")


async def test_mock_run_streams_full_event_set_over_sse(server) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        r = await client.post("/api/runs/web-mock-1/start", json={"kind": "mock"})
        assert r.status_code == 200 and r.json()["started"] is True
        seen: set = set()
        await asyncio.wait_for(
            _collect_sse(client, "web-mock-1", seen, EventType.RUN_FINISHED.value),
            timeout=25,
        )
    assert EventType.RUN_STARTED.value in seen
    assert EventType.REASONING_DELTA.value in seen
    assert EventType.TOOL_CALL_RESULT.value in seen
    assert EventType.SOLVE_GRAPH_DELTA.value in seen
    assert EventType.COST_UPDATE.value in seen
    assert EventType.RUN_FINISHED.value in seen


async def test_mock_run_finished_carries_terminal_reason(server) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        r = await client.post(
            "/api/runs/web-mock-reason/start",
            json={"kind": "mock", "expected_flags": 2},
        )
        assert r.status_code == 200 and r.json()["started"] is True
        payload = await asyncio.wait_for(
            _collect_sse_payload(
                client, "web-mock-reason", EventType.RUN_FINISHED.value),
            timeout=25,
        )
    assert payload["reason"] == "goal_met"
    assert payload["flags"] == ["flag{mock_encoding_solved}", "flag{mock_part_2}"]


async def test_credentials_endpoint_reads_shared_graph(tmp_path) -> None:
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("cred-run")
    graph_dir = mgr.workspace_dir(run.run_id) / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = SQLiteSharedGraph.open(
        db_path=graph_dir / "shared_graph.db",
        challenge=Challenge(id="cred-run", name="cred", category="web"),
    )
    graph.add_evidence(
        actor="cli-a",
        source="curl",
        fact="admin password hunter2 successfully logs in as admin",
        verified=True,
    )
    graph.close()
    app = create_app(mgr)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        resp = await client.get("/api/runs/cred-run/credentials")

    assert resp.status_code == 200
    creds = resp.json()["credentials"]
    assert len(creds) == 1
    assert creds[0]["entity"] == "admin"
    assert creds[0]["value"] == "hunter2"


async def test_btw_endpoint_streams_one_shot_worker_without_swarm_slot(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("DSWARM_WEB_BIND", raising=False)
    mgr = RunManager(sessions_root=tmp_path)
    profiles = mgr.worker_config.get()["worker_profiles"]
    next(p for p in profiles if p["name"] == "pi-worker")["enabled"] = True
    mgr.worker_config.set(worker_profiles=profiles, engines=["pi-worker"])
    run = mgr.create("btw-run")
    run.name = "btw demo"
    run.category = "web"
    # UI does not pass profile/engine. Even if a historical winner exists, /btw
    # should default to the configured review profile so it behaves like review
    # without consuming a review/swarm slot.
    root = mgr.workspace_dir(run.run_id)
    (root / "winner.json").write_text(
        json.dumps({"engine": "pi-worker"}),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        "apps.web.worker_config.backend_for_profile",
        lambda *args, **kwargs: "local",
    )

    async def fake_stream_btw_worker_deltas(**kwargs):
        seen.update(kwargs)
        yield "worker "
        yield "answer"

    monkeypatch.setattr(
        "dswarm.solver.btw.stream_btw_worker_deltas",
        fake_stream_btw_worker_deltas,
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        resp = await client.post(
            "/api/runs/btw-run/btw",
            json={
                "question": "本轮问题",
                "transcript": [
                    {"role": "user", "content": "上一问"},
                    {"role": "assistant", "content": "上一答"},
                ],
                "worker_backend": "local",
            },
        )

    assert resp.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [f.get("delta") for f in frames if f.get("delta")] == [
        "worker ",
        "answer",
    ]
    assert frames[0] == {"status": "正在读取 run 证据…"}
    assert frames[-1] == {"done": True}
    prompt = str(seen["prompt"])
    assert "本轮问题" in prompt
    assert "上一问" in prompt
    assert "上一答" in prompt
    assert "shared_graph.db" in prompt
    assert seen["web_access"] is False
    assert seen["kb_access"] is False
    assert seen["env"]["DSWARM_BTW_WORKER"] == "1"  # type: ignore[index]
    assert getattr(seen["driver"], "profile", {}).get("name") == "pi-worker"
    # cwd is the _btw worker dir; separators are platform-specific
    assert ("workers" + os.sep + "_btw") in str(seen["cwd"])
    assert run.worker_cmds.empty()


async def test_btw_default_uses_evidence_pack_and_authoritative_final(tmp_path, monkeypatch):
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("DSWARM_WEB_BIND", raising=False)
    monkeypatch.setenv("DSWARM_DEEPSEEK_API_KEY", "test-key")
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("btw-evidence-run")
    run.name = "evidence demo"
    run.category = "web"
    seen: dict[str, object] = {}

    class FakeResponse:
        content = '{"answer_markdown":"已读取 **只读证据**。","evidence_refs":[],"uncertainties":[],"answer_type":"summary"}'
        finish_reason = "stop"

    class FakeLLM:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def chat(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            seen["model"] = kwargs["model"]
            seen["stream"] = kwargs["stream"]
            seen["max_tokens"] = kwargs["max_tokens"]
            return FakeResponse()

    monkeypatch.setattr("dswarm.core.llm.LLMClient", FakeLLM)
    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10, trust_env=False
    ) as client:
        resp = await client.post("/api/runs/btw-evidence-run/btw", json={"question": "总结"})

    assert resp.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines() if line.startswith("data: ")
    ]
    assert frames[0]["status"] == "正在整理只读证据…"
    assert frames[-2]["final"] == "已读取 **只读证据**。"
    assert frames[-1] == {"done": True}
    assert not any("delta" in frame for frame in frames)
    assert seen["model"] == "deepseek-v4-flash"
    assert seen["stream"] is False
    assert seen["max_tokens"] is None
    assert "EVIDENCE_PACK" in str(seen["messages"][0]["content"])
    assert run.worker_cmds.empty()


async def test_btw_readonly_failure_is_one_user_facing_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("DSWARM_WEB_BIND", raising=False)
    mgr = RunManager(sessions_root=tmp_path)
    mgr.create("btw-failure-run")

    def fail_pack(**kwargs):
        raise TimeoutError()

    monkeypatch.setattr("dswarm.solver.btw.build_btw_evidence_pack_sync", fail_pack)
    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        resp = await client.post(
            "/api/runs/btw-failure-run/btw",
            json={"question": "总结当前进展"},
        )

    assert resp.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    failure = frames[-2]
    assert "final" in failure
    assert "error" not in failure
    assert "请求超时" in failure["final"]
    assert frames[-1] == {"done": True}



async def test_btw_readonly_httpx_read_timeout_is_normalized(tmp_path, monkeypatch):
    """httpx.ReadTimeout has an empty str(); never expose the raw class name."""
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("DSWARM_WEB_BIND", raising=False)
    monkeypatch.setenv("DSWARM_DEEPSEEK_API_KEY", "test-key")
    mgr = RunManager(sessions_root=tmp_path)
    mgr.create("btw-read-timeout-run")

    class FailingLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def chat(self, **kwargs):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr("dswarm.core.llm.LLMClient", FailingLLM)
    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        resp = await client.post(
            "/api/runs/btw-read-timeout-run/btw",
            json={"question": "总结当前进展"},
        )

    assert resp.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    failure = frames[-2]
    assert "只读总结请求超时" in failure["final"]
    assert "ReadTimeout" not in failure["final"]
    assert "error" not in failure
    assert frames[-1] == {"done": True}


async def test_btw_container_reuses_gateway_token_and_sets_pi_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("DSWARM_WEB_BIND", raising=False)
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("btw-container-run")
    run.name = "btw container demo"
    run.category = "web"

    class FakeContainer:
        def to_container_path(self, path: str) -> str:
            return "/workspace/" + Path(path).name

    class FakeGateway:
        account_root = None
        sessions_root = None

        def token_for_run(self, run_id: str) -> str:
            assert run_id == "btw-container-run"
            return "reused-task-token"

        def issue(self, run_id: str) -> str:
            raise AssertionError("BTW must not revoke an active run token")

    fake_gateway = FakeGateway()
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        "apps.web.worker_config.backend_for_profile",
        lambda *args, **kwargs: "container",
    )
    monkeypatch.setattr(
        "dswarm.solver.container_exec.ensure_container",
        lambda *args, **kwargs: FakeContainer(),
    )
    monkeypatch.setattr(
        "dswarm.solver.container_exec._chown_tree_to_worker",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dswarm.solver.modelgateway.ModelGateway.instance",
        staticmethod(lambda: fake_gateway),
    )

    async def fake_stream_btw_worker_deltas(**kwargs):
        seen.update(kwargs)
        yield "ok"

    monkeypatch.setattr(
        "dswarm.solver.btw.stream_btw_worker_deltas",
        fake_stream_btw_worker_deltas,
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        resp = await client.post(
            "/api/runs/btw-container-run/btw",
            json={"question": "为什么这么久", "worker_backend": "container"},
        )

    assert resp.status_code == 200
    env = seen["env"]
    assert env["DSWARM_TASK_TOKEN"] == "reused-task-token"  # type: ignore[index]
    assert env["DEEPSEEK_API_KEY"] == "reused-task-token"  # type: ignore[index]
    assert env["DSWARM_PI_PROVIDER"] == "ctf-gateway"  # type: ignore[index]
    assert env["DSWARM_GATEWAY_URL"].endswith("/v1")  # type: ignore[index]
    assert env["DSWARM_WORKER_MODEL"] == "deepseek-v4-flash"  # type: ignore[index]
    assert seen["container"] is not None

async def test_hitl_post_is_accepted_and_echoed(server) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        # idle run keeps the bus open so the HITL echo is observable (a fast mock
        # run could close the stream before we post)
        await client.post("/api/runs/hitl-run/start", json={"kind": "idle"})
        seen: set = set()
        # the HITL_RESPONSE should show up on the stream after we POST it
        watcher = asyncio.create_task(
            _collect_sse(client, "hitl-run", seen, EventType.HITL_RESPONSE.value)
        )
        await asyncio.sleep(0.2)
        r = await client.post(
            "/api/runs/hitl-run/hitl",
            json={"target": "solver:mock-flash", "action": "hint", "text": "try base64"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        await asyncio.wait_for(watcher, timeout=15)
    assert EventType.HITL_RESPONSE.value in seen


async def test_non_dict_body_is_rejected_with_400(server) -> None:
    """Finding #7: a non-object JSON body (e.g. json=[]) must be a clean 400 on every
    write route — NOT an opaque 500 (/hitl, /start, folders, workers) and NOT a silent
    200 that swallows the request (PATCH /api/runs). Critically, /hitl 500'ing meant an
    operator literally could not STOP a run with a malformed client."""
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        await client.post("/api/runs/badbody-run/start", json={"kind": "idle"})
        for method, path in [
            ("POST", "/api/runs/badbody-run/hitl"),
            ("PATCH", "/api/runs/badbody-run"),
            ("POST", "/api/runs/badbody-run/start"),
            ("POST", "/api/folders"),
            ("PUT", "/api/settings/workers"),
            ("POST", "/api/settings/workers/probe"),
            ("POST", "/api/settings/worker-model/test"),
            ("POST", "/api/runs/badbody-run/workers"),
            ("DELETE", "/api/runs/badbody-run/workers"),
        ]:
            r = await client.request(method, path, json=[])
            assert r.status_code == 400, f"{method} {path} should 400 on a list body, got {r.status_code}"


async def test_legacy_swarm_fields_are_rejected_with_400(server) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        r = await client.post(
            "/api/runs/legacy-start/start",
            json={"kind": "swarm", "challenge": {"description": "solve"}, "race_scout": True},
        )
        assert r.status_code == 400
        assert "legacy swarm fields" in r.text


async def test_worker_model_options_and_probe_routes(server, monkeypatch) -> None:
    from apps.web import worker_models

    seen = {}

    def fake_probe_worker_model(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "detail": "模型可用", "engine": "pi", "model": kwargs["model"]}

    monkeypatch.setattr(worker_models, "probe_worker_model", fake_probe_worker_model)

    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        opts = await client.get("/api/settings/worker-models")
        assert opts.status_code == 200
        assert opts.json()["allow_custom"] is True
        assert "pi" in opts.json()["models"]

        r = await client.post("/api/settings/worker-model/test", json={
            "profile": {"id": "pi-sub", "engine": "pi", "credential_account": "pi-main"},
            "model": "deepseek-v4-pro",
            "backend": "local",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert seen["model"] == "deepseek-v4-pro"
        assert seen["profile"]["id"] == "pi-sub"


async def test_worker_endpoint_probe_uses_unsaved_key_without_echoing_it(server, monkeypatch) -> None:
    from apps.web import worker_endpoint

    seen = {}

    def fake_probe_worker_endpoint(profile, *, api_key, validate_model=False):
        seen["profile"] = profile
        seen["api_key"] = api_key
        seen["validate_model"] = validate_model
        return {
            "ok": False,
            "detail": "服务器已收到请求，但认证失败，请检查 API Key 和认证方式。",
            "error_layer": "authentication",
            "models": [],
            "authentication": {"ok": False, "status": 401},
        }

    monkeypatch.setattr(worker_endpoint, "probe_worker_endpoint", fake_probe_worker_endpoint)
    draft_key = "draft-secret-never-echo"
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        response = await client.post("/api/settings/workers/probe", json={
            "profile": {
                "id": "pi-web",
                "engine": "pi",
                "base_url": "https://api.example.test/v1",
                "wire_api": "openai-responses",
                "auth_mode": "x-api-key",
                "model": "example-model",
            },
            "api_key": draft_key,
            "validate_model": True,
        })

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_layer"] == "authentication"
    assert payload["authentication"]["status"] == 401
    assert draft_key not in response.text
    assert seen["api_key"] == draft_key
    assert seen["validate_model"] is True
    assert seen["profile"]["auth_mode"] == "x-api-key"


async def test_engine_health_route_passes_enabled_worker_profiles(tmp_path, monkeypatch) -> None:
    import dswarm.solver.cli_driver as cli_driver

    seen = {}

    def fake_engine_health(backend="local", account_root=None, profiles=None):
        seen["backend"] = backend
        seen["profiles"] = profiles
        return [{"engine": "pi", "healthy": True, "backend": backend}]

    monkeypatch.setattr(cli_driver, "engine_health", fake_engine_health)
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    mgr.worker_config.set(
        engines=["pi-main"],
        worker_backend="local",
        worker_profiles=[
            {"id": "pi-main", "name": "pi-main", "engine": "pi",
             "transport": "pi_cli", "credential_account": "",
             "runtime": "local", "model": "deepseek-v4-flash"},
            {"id": "pi-aux", "name": "pi-aux", "engine": "pi",
             "transport": "pi_cli", "credential_account": "",
             "runtime": "local", "model": "deepseek-v4-pro"},
        ],
    )
    app = create_app(mgr)
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=10, trust_env=False) as client:
            r = await client.get("/api/engines/health?backend=local")
    assert r.status_code == 200
    assert seen["backend"] == "local"
    assert [p["id"] for p in seen["profiles"]] == ["pi-main"]
    assert seen["profiles"][0]["model"] == "deepseek-v4-flash"


async def test_worker_routes_accept_empty_body(server) -> None:
    """Finding #7: the worker spawn/kill routes legitimately accept NO body ('let the
    coordinator pick the engine'), so an empty/absent body must still be a 200 — only a
    present-but-non-object body is rejected."""
    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        await client.post("/api/runs/empty-body-run/start", json={"kind": "idle"})
        # no json= → empty body
        r = await client.post("/api/runs/empty-body-run/workers")
        assert r.status_code == 200
        r = await client.post("/api/runs/empty-body-run/workers", json={})
        assert r.status_code == 200


async def test_engines_endpoint_singleflights_slow_probe(tmp_path, monkeypatch) -> None:
    """A slow engine-status refresh must not stack duplicate CLI hello probes when
    the deck or multiple tabs hit /api/engines concurrently."""
    import dswarm.solver.cli_driver as cli_driver

    calls = 0
    calls_lock = threading.Lock()

    def fake_engine_status(account_root=None, backend="local", profiles=None):
        nonlocal calls
        with calls_lock:
            calls += 1
        assert profiles is not None
        time.sleep(0.2)
        return [{"engine": "pi", "available": True, "healthy": True}]

    monkeypatch.setattr(cli_driver, "engine_status", fake_engine_status)
    app = create_app(RunManager(sessions_root=str(tmp_path / "sessions")))
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=10, trust_env=False) as client:
            responses = await asyncio.gather(
                client.get("/api/engines"),
                client.get("/api/engines"),
                client.get("/api/engines"),
            )
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert calls == 1


async def test_engines_endpoint_passes_enabled_worker_profiles(tmp_path, monkeypatch) -> None:
    """The top engine bar must probe the configured worker profile/model, not a
    bare engine default. Otherwise Claude can be shown as down when the default
    model is exhausted but the selected Sonnet profile is usable."""
    import dswarm.solver.cli_driver as cli_driver

    seen = {}

    def fake_engine_status(account_root=None, backend="local", profiles=None):
        seen["backend"] = backend
        seen["profiles"] = profiles
        return [{"engine": "pi", "available": True, "healthy": True}]

    monkeypatch.setattr(cli_driver, "engine_status", fake_engine_status)
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    mgr.worker_config.set(
        engines=["pi-main"],
        worker_backend="local",
        worker_profiles=[
            {"id": "pi-main", "name": "pi-main", "engine": "pi",
             "transport": "pi_cli", "credential_account": "",
             "runtime": "local", "model": "deepseek-v4-flash"},
            {"id": "pi-aux", "name": "pi-aux", "engine": "pi",
             "transport": "pi_cli", "credential_account": "",
             "runtime": "local", "model": "deepseek-v4-pro"},
        ],
    )
    app = create_app(mgr)
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=10, trust_env=False) as client:
            r = await client.get("/api/engines")

    assert r.status_code == 200
    assert seen["backend"] == "local"
    assert [p["id"] for p in seen["profiles"]] == ["pi-main"]
    assert seen["profiles"][0]["model"] == "deepseek-v4-flash"


async def test_credential_account_api_keeps_secret_write_only_and_persists(tmp_path) -> None:
    """Credential routes expose presence/endpoint metadata, never raw keys."""
    sessions = tmp_path / "sessions"
    app = create_app(RunManager(sessions_root=str(sessions)))
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=15, trust_env=False) as client:
            r = await client.put(
                "/api/settings/credential-accounts/pi-team",
                json={"engine": "pi", "secret": "super-secret-token"},
            )
            assert r.status_code == 200
            account = r.json()["account"]
            assert account["engine"] == "pi"
            assert account["details"]["has_secret"] is True
            assert "secret_value" not in account["details"]
            assert "super-secret-token" not in r.text

            listed = await client.get("/api/settings/credential-accounts")
            assert listed.status_code == 200
            accounts = listed.json()["accounts"]
            assert accounts[0]["account_id"] == "pi-team"
            assert accounts[0]["present"] is True
            assert accounts[0]["details"]["has_secret"] is True
            assert "secret_value" not in accounts[0]["details"]
            assert "super-secret-token" not in listed.text
            assert (
                sessions / "_secrets" / "accounts" / "pi-team" / "API_KEY"
            ).read_text(encoding="utf-8").strip() == "super-secret-token"

            api = await client.put(
                "/api/settings/credential-accounts/deepseek-main",
                json={
                    "engine": "api",
                    "secret": "deepseek-secret",
                    "base_url": "https://api.deepseek.example/v1",
                },
            )
            assert api.status_code == 200
            api_account = api.json()["account"]
            assert api_account["details"]["has_secret"] is True
            assert "secret_value" not in api_account["details"]
            assert "deepseek-secret" not in api.text
            assert api_account["details"]["base_url"] is True
            assert (
                api_account["details"]["base_url_value"]
                == "https://api.deepseek.example/v1"
            )

            bad = await client.put(
                "/api/settings/credential-accounts/bad/cut",
                json={"engine": "pi", "secret": "x"},
            )
            assert bad.status_code == 404


async def test_settings_redesign_endpoints(tmp_path) -> None:
    """The four new settings endpoints (DESIGN §2.3/§2.4/§5) exist and behave:
    system-login (read-only), runtime-environment write-back, account test
    (no-creds → ok:false), llm test (bogus → ok:false, no network success)."""
    app = create_app(RunManager(sessions_root=str(tmp_path / "sessions")))
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=20, trust_env=False) as client:
            # system-login: returns a status per engine, never errors
            sl = await client.get("/api/settings/system-login")
            assert sl.status_code == 200
            logins = sl.json()["logins"]
            assert set(logins) == {"pi"}
            assert all(v in ("present", "absent", "unknown") for v in logins.values())

            # runtime-environment: flip to local, all profiles follow
            rt = await client.put("/api/settings/runtime-environment",
                                  json={"backend": "local", "runtime_id": "local"})
            assert rt.status_code == 200
            cfg = rt.json()["config"]
            assert cfg["worker_backend"] == "local"
            assert all(p["runtime"] == "local" for p in cfg["worker_profiles"])
            # mismatch rejected
            bad = await client.put("/api/settings/runtime-environment",
                                   json={"backend": "local", "runtime_id": "docker-web"})
            assert bad.status_code == 400

            # account test: unregistered account → ok:false, no host fallback
            at = await client.post(
                "/api/settings/credential-accounts/ghost/test",
                json={"engine": "pi", "backend": "local"})
            assert at.status_code == 200
            assert at.json()["ok"] is False

            # llm test: empty model → ok:false (no network needed)
            lt = await client.post("/api/settings/llm/test",
                                   json={"which": "planner", "model": ""})
            assert lt.status_code == 200
            assert lt.json()["ok"] is False


async def test_events_opens_for_not_yet_started_run(server) -> None:
    # A deck opens its event stream BEFORE launching a run. That must NOT 404 —
    # the endpoint creates the run handle on demand and holds the SSE open so it
    # streams the moment the run starts (no reconnect race). We confirm the
    # stream opens with 200 (then close it without waiting for events).
    async with httpx.AsyncClient(base_url=server.base, timeout=10, trust_env=False) as client:
        async with client.stream("GET", "/api/runs/not-started-yet/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
        # The run handle now exists in memory, but /api/runs is STARTED-ONLY: a
        # merely-subscribed (never-dispatched) run is a draft stub and must NOT
        # clutter the thread rail. It appears only once it is /start-ed.
        r = await client.get("/api/runs")
        run_ids = [row["run_id"] for row in r.json()["runs"]]
        assert "not-started-yet" not in run_ids


async def test_ws_terminal_streams_sandbox_output(server) -> None:
    import websockets

    async with httpx.AsyncClient(base_url=server.base, timeout=30, trust_env=False) as client:
        await client.post("/api/runs/ws-run/start", json={"kind": "mock"})
    ws_url = server.base.replace("http://", "ws://") + "/api/runs/ws-run/terminal"
    got: list[str] = []
    async with websockets.connect(ws_url) as ws:
        # the mock emits two TERMINAL_OUTPUT lines; replay-from-0 delivers them
        try:
            for _ in range(2):
                got.append(await asyncio.wait_for(ws.recv(), timeout=10))
        except asyncio.TimeoutError:
            pass
    joined = "".join(got)
    assert "GET /secret" in joined or "auto_decode" in joined


async def test_fresh_subscriber_replays_full_history_past_ring_overflow(tmp_path) -> None:
    """A deck opening a LONG run (more events than the in-memory ring holds) must
    still receive run.started — the first event — by replaying the durable
    SessionStore history, not just the truncated ring. Without this, a deck that
    connects mid/post-run never leaves the empty state (the real bug seen while
    backtesting a long web challenge)."""
    from dswarm.core.event_bus import EventBus
    from dswarm.core.events import Event

    # a run whose bus has a TINY ring so we overflow it cheaply
    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("long-run")
    # swap in a small-ring bus that still persists to the same store sink
    small = EventBus(ring_size=8)
    small.add_sink(run.store.sink)
    run.bus = small

    # emit run.started (seq 1) then enough events to evict it from the ring
    await small.emit(Event(event_type=EventType.RUN_STARTED, run_id="long-run",
                           payload={"challenge": {"name": "long", "category": "web"}}))
    for i in range(40):  # >> ring_size(8) -> run.started is long gone from the ring
        await small.emit(Event(event_type=EventType.REASONING_DELTA, run_id="long-run",
                               payload={"text": f"step {i} "}))

    app = create_app(manager)
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=15, trust_env=False) as client:
            seen: list[str] = []
            # fresh subscribe (no Last-Event-ID): must replay from the store
            async with client.stream("GET", "/api/runs/long-run/events") as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        seen.append(line.split(":", 1)[1].strip())
                    # we have the full history once we've seen the first event +
                    # several deltas; stop so the test doesn't hang on the live tail
                    if len(seen) >= 41:
                        break
    # the very first event survived ring overflow via the durable replay
    assert seen[0] == EventType.RUN_STARTED.value
    assert seen.count(EventType.REASONING_DELTA.value) == 40


@pytest.mark.asyncio
async def test_sse_reconnect_replays_monotonic_history_after_seq_reset(tmp_path) -> None:
    """A browser reconnecting with Last-Event-ID must not miss a post-restart segment.

    Regression for runs whose JSONL contained raw seq reset after continue/reopen
    (1,2,1,2). The SSE layer normalizes that to 1,2,3,4 and replays events after
    the client's cursor.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    run_id = "run-reset"
    events = [
        {"event_type": "run.started", "seq": 1, "ts": 1.0, "run_id": run_id,
         "payload": {"challenge": {"name": "reset", "category": "web"}}},
        {"event_type": "reasoning.delta", "seq": 2, "ts": 2.0, "run_id": run_id,
         "payload": {"text": "before"}},
        {"event_type": "run.reopened", "seq": 1, "ts": 3.0, "run_id": run_id,
         "payload": {"reason": "resolve"}},
        {"event_type": "reasoning.delta", "seq": 2, "ts": 4.0, "run_id": run_id,
         "payload": {"text": "after"}},
    ]
    with (sessions / f"{run_id}.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    app = create_app(RunManager(sessions_root=str(sessions)))
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=15, trust_env=False) as client:
            ids: list[str] = []
            seen: list[str] = []
            async with client.stream(
                "GET", f"/api/runs/{run_id}/events",
                headers={"Last-Event-ID": "2"},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("id:"):
                        ids.append(line.split(":", 1)[1].strip())
                    elif line.startswith("event:"):
                        seen.append(line.split(":", 1)[1].strip())
                    if len(seen) >= 2:
                        break

    assert ids[:2] == ["3", "4"]
    assert seen[:2] == [EventType.RUN_REOPENED.value, EventType.REASONING_DELTA.value]


@pytest.mark.asyncio
async def test_upload_lands_in_run_dir_and_returns_abs_path(tmp_path) -> None:
    """A file POSTed to /uploads lands in sessions/{id}/uploads/ and the endpoint
    returns its ABSOLUTE path — exactly what challenge.attachments needs so the
    worker can stage it into its cwd."""
    sessions = tmp_path / "sessions"
    app = create_app(RunManager(sessions_root=str(sessions)))
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=15, trust_env=False) as client:
            files = {"files": ("cipher.txt", b"deadbeef", "text/plain")}
            r = await client.post("/api/runs/run-0001/uploads", files=files)
            assert r.status_code == 200
            saved = r.json()["files"]
            assert len(saved) == 1
            assert saved[0]["name"] == "cipher.txt"
            assert saved[0]["size"] == 8
            p = Path(saved[0]["path"])
            assert p.is_absolute() and p.exists()
            assert p.read_bytes() == b"deadbeef"
            # lands under sessions/<run_id>/uploads/, beside (not colliding with)
            # the run's {id}.jsonl log
            assert p.parent == (sessions / "run-0001" / "uploads")


@pytest.mark.asyncio
async def test_upload_sanitizes_path_traversal_filename(tmp_path) -> None:
    """A hostile filename (path traversal / absolute) is reduced to its basename
    and can never escape the run's uploads dir."""
    sessions = tmp_path / "sessions"
    app = create_app(RunManager(sessions_root=str(sessions)))
    uploads = sessions / "run-0002" / "uploads"
    with _Server(app) as srv:
        async with httpx.AsyncClient(base_url=srv.base, timeout=15, trust_env=False) as client:
            files = {"files": ("../../etc/evil", b"x", "application/octet-stream")}
            r = await client.post("/api/runs/run-0002/uploads", files=files)
            assert r.status_code == 200
            p = Path(r.json()["files"][0]["path"])
            assert p.parent == uploads          # stayed inside the run's folder
            assert p.name == "evil"             # only the basename survived
            assert not (sessions.parent / "etc" / "evil").exists()


@pytest.mark.asyncio
async def test_resolve_uses_scheduler_launch_and_finishes_mock(tmp_path) -> None:
    """Continue must use the same scheduler/launch lifecycle as /start.

    Regression for recovery runs that emitted only run.reopened: the old direct
    create_task path bypassed the scheduler and had no terminal error guard.
    """
    from dswarm.core.events import Event

    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = mgr.create("resolve-run")
    await run.bus.emit(Event(
        event_type=EventType.RUN_STARTED, run_id=run.run_id,
        payload={"challenge": {"name": "resolve", "category": "web"}}))
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED, run_id=run.run_id,
        payload={"solved": False, "reason": "finished"}))

    assert await mgr.resolve(run.run_id, {"kind": "mock", "tick": 0.001})
    assert run.started is True
    assert mgr.scheduler.active_count == 1
    await asyncio.wait_for(run.task, timeout=5)

    assert run.finished is True
    assert mgr.scheduler.active_count == 0
    events = [ev async for ev in run.store.replay(run.run_id)]
    kinds = [ev.event_type for ev in events]
    assert EventType.RUN_REOPENED in kinds
    assert EventType.RUN_FINISHED in kinds


@pytest.mark.asyncio
async def test_resolve_driver_failure_emits_terminal_runtime_failure(tmp_path, monkeypatch) -> None:
    """A recovery driver crash must be durable and observable, never a ghost run."""
    from dswarm.core.events import Event

    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = mgr.create("resolve-failure")
    await run.bus.emit(Event(
        event_type=EventType.RUN_STARTED, run_id=run.run_id,
        payload={"challenge": {"name": "failure", "category": "web"}}))
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED, run_id=run.run_id,
        payload={"solved": False, "reason": "finished"}))

    async def boom(_run):
        raise RuntimeError("worker bootstrap exploded")

    monkeypatch.setattr("apps.web.drivers.build_driver", lambda _body, mgr=None: boom)
    assert await mgr.resolve(run.run_id, {})
    await asyncio.wait_for(run.task, timeout=5)

    events = [ev async for ev in run.store.replay(run.run_id)]
    terminal = [ev for ev in events if ev.event_type is EventType.RUN_FINISHED][-1]
    assert terminal.payload["reason"] == "runtime_failure"
    assert "worker bootstrap exploded" in terminal.payload["detail"]
    assert run.finished is True
    assert mgr.scheduler.active_count == 0


@pytest.mark.asyncio
async def test_resolve_infers_pre_sidecar_roster_from_worker_history(tmp_path, monkeypatch) -> None:
    """Old runs without a dispatch sidecar must not silently use today's roster."""
    from dswarm.core.events import Event

    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = mgr.create("legacy-resolve")
    await run.bus.emit(Event(
        event_type=EventType.RUN_STARTED, run_id=run.run_id,
        payload={"challenge": {"name": "legacy", "category": "web"}}))
    await run.bus.emit(Event(
        event_type=EventType.WORKER_STATUS, run_id=run.run_id,
        payload={"online": True, "engine": "pi",
                 "runtime": {"backend": "container"}}))
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED, run_id=run.run_id,
        payload={"solved": False, "reason": "finished"}))

    captured: dict = {}
    async def driver(_run):
        captured.update(_run.manager_dispatch if hasattr(_run, "manager_dispatch") else {})
        await _run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED, run_id=_run.run_id,
            payload={"solved": False, "reason": "mock"}))

    def build(body, mgr=None):
        captured.update(body)
        return driver

    monkeypatch.setattr("apps.web.drivers.build_driver", build)
    assert await mgr.resolve(run.run_id, {"kind": "mock"})
    await asyncio.wait_for(run.task, timeout=5)
    # the historical base engine "pi" is recovered as the category's direction
    # profile (web), keeping old-run resolve on a single worker
    assert captured["engines"] == ["pi-web"]
    assert captured["worker_backend"] == "container"
    assert "race_scout" not in captured


@pytest.mark.asyncio
async def test_resolve_strips_legacy_fields_from_saved_sidecar(tmp_path, monkeypatch) -> None:
    from dswarm.core.events import Event

    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = mgr.create("legacy-sidecar")
    await run.bus.emit(Event(
        event_type=EventType.RUN_STARTED, run_id=run.run_id,
        payload={"challenge": {"name": "legacy", "category": "web"}}))
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED, run_id=run.run_id,
        payload={"solved": False, "reason": "finished"}))
    mgr.remember_dispatch(run.run_id, {
        "kind": "mock", "race_scout": False, "cold_start": False,
        "stage_policy": {"race": {"enabled": False}, "budgets": {"max_total_workers": 2}},
    })
    captured: dict = {}
    async def driver(_run):
        await _run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED, run_id=_run.run_id,
            payload={"solved": False, "reason": "mock"}))
    def build(body, mgr=None):
        captured.update(body)
        return driver
    monkeypatch.setattr("apps.web.drivers.build_driver", build)
    assert await mgr.resolve(run.run_id, {})
    await asyncio.wait_for(run.task, timeout=5)
    assert "race_scout" not in captured
    assert "cold_start" not in captured
    assert "race" not in captured["stage_policy"]


@pytest.mark.asyncio
async def test_dispatch_settings_survive_resolve_and_redact_secrets(tmp_path) -> None:
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = mgr.create("dispatch-config")
    saved = mgr.remember_dispatch(run.run_id, {
        "kind": "swarm", "worker_backend": "container",
        "engines": ["pi"], "worker_profiles": [{"name": "pi", "credential_account": "acct"}],
        "api_key": "must-not-persist",
    })
    assert saved["worker_backend"] == "container"
    assert "api_key" not in saved
    assert (mgr.workspace_dir(run.run_id) / ".dswarm_dispatch.json").exists()

    mgr2 = RunManager(sessions_root=str(tmp_path / "sessions"))
    restored = mgr2.create(run.run_id)
    assert restored.dispatch_body["worker_backend"] == "container"
    assert "api_key" not in restored.dispatch_body


# 👻👻 ghost-running guard: a run whose durable history ENDS on run.started with no
# live task must get a synthetic RUN_FINISHED on stream open, so the deck settles
# to "finished" (not stuck on running — only Stop shown). This is the run-4305 fix.
@pytest.fixture
def server_mgr():
    mgr = RunManager(sessions_root="/tmp/dswarm_web_ghost_sessions")
    app = create_app(mgr)
    with _Server(app) as s:
        yield s, mgr


async def test_events_injects_run_finished_for_ghost_run(server_mgr) -> None:
    from dswarm.core.events import Event
    s, mgr = server_mgr
    rid = "ghost-run-1"
    run = mgr.create(rid)
    # seed durable history that ENDS on run.started (the ghost shape) — no finish.
    await run.bus.emit(Event(event_type=EventType.RUN_STARTED, run_id=rid,
                             payload={"challenge": {"name": "x"}}))
    await run.bus.emit(Event(event_type=EventType.REASONING_DELTA, run_id=rid,
                             payload={"text": "working...\n"}))
    run.started = True
    run.finished = False
    run.task = None  # dead task — ghost
    # opening a FRESH event stream must replay history THEN inject RUN_FINISHED.
    async with httpx.AsyncClient(base_url=s.base, timeout=30, trust_env=False) as client:
        seen: set = set()
        await asyncio.wait_for(
            _collect_sse(client, rid, seen, EventType.RUN_FINISHED.value), timeout=15)
    assert EventType.RUN_STARTED.value in seen
    assert EventType.RUN_FINISHED.value in seen  # the synthetic terminator


async def test_finished_event_stream_stays_open_after_replay(server_mgr) -> None:
    from dswarm.core.events import Event
    s, mgr = server_mgr
    rid = f"finished-run-{uuid.uuid4().hex}"
    run = mgr.create(rid)
    await run.bus.emit(Event(event_type=EventType.RUN_STARTED, run_id=rid,
                             payload={"challenge": {"name": "x"}}))
    await run.bus.emit(Event(event_type=EventType.RUN_FINISHED, run_id=rid,
                             payload={"solved": False}))
    run.started = True
    run.finished = True
    await run.bus.close()

    async with httpx.AsyncClient(base_url=s.base, timeout=30, trust_env=False) as client:
        async with client.stream("GET", f"/api/runs/{rid}/events") as resp:
            assert resp.status_code == 200
            lines = resp.aiter_lines()
            seen_finished = False
            in_finished = False
            async for line in lines:
                if line.startswith("event:") and EventType.RUN_FINISHED.value in line:
                    in_finished = True
                elif in_finished and line == "":
                    seen_finished = True
                    break
            assert seen_finished

            async def next_nonempty_line_or_closed() -> str:
                try:
                    while True:
                        line = await lines.__anext__()
                        if line:
                            return line
                except StopAsyncIteration:
                    return "__closed__"

            # A closed response makes EventSource reconnect forever. The stream
            # should instead stay idle/pending after replay (ping frames arrive
            # later, outside this short window).
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(next_nonempty_line_or_closed(), timeout=0.25)


def test_rehydrate_force_settles_started_unfinished_run(tmp_path) -> None:
    # ghost run: a run whose on-disk summary says started=True but finished=False
    # (killed mid-run before RUN_FINISHED). On restart, _rehydrate has no live task,
    # so it MUST settle it to finished — else the rail spins forever.
    from dswarm.core.events import Event
    sessions = tmp_path / "sessions"
    mgr1 = RunManager(sessions_root=str(sessions))
    run = mgr1.create("ghost-2")

    async def seed() -> None:
        await run.bus.emit(Event(event_type=EventType.RUN_STARTED, run_id="ghost-2",
                                 payload={"challenge": {"name": "x"}}))
        run.started = True
        run.finished = False
        await run.bus.close()
    asyncio.run(seed())

    # a fresh manager rehydrates from disk — the started-but-unfinished run settles.
    mgr2 = RunManager(sessions_root=str(sessions))
    r2 = mgr2.runs.get("ghost-2")
    assert r2 is not None
    assert r2.started is True
    assert r2.finished is True   # force-settled (was a ghost otherwise)


def test_start_finally_emits_run_finished_on_cancel(tmp_path) -> None:
    # if a driver is CANCELLED mid-run (server restart, manual cancel) before it
    # emits its own RUN_FINISHED, the _go finally must synthesize one so the deck
    # gets a terminal event (no infinite spinner).
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    seen: list = []

    async def go() -> None:
        async def driver(run) -> None:
            run.bus.add_sink(lambda ev: seen.append(ev.event_type))
            await asyncio.sleep(10)   # long-running; we cancel it before it finishes

        run = await mgr.start("cancel-run", driver)
        await asyncio.sleep(0.05)
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        assert run.finished is True
        assert EventType.RUN_FINISHED in seen  # synthesized in the finally

    asyncio.run(go())


def test_start_finally_includes_runtime_failure_detail(tmp_path) -> None:
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    seen: list[dict] = []

    async def go() -> None:
        async def sink(ev) -> None:
            if ev.event_type is EventType.RUN_FINISHED:
                seen.append(ev.payload)

        async def driver(run) -> None:
            run.bus.add_sink(sink)
            raise RuntimeError("profile_unhealthy missing credential account(s): pi-api-local:pi-main")

        run = await mgr.start("failed-run", driver)
        await run.task
        assert run.finished is True

    asyncio.run(go())
    assert seen
    assert seen[-1]["reason"] == "runtime_failure"
    assert "pi-api-local:pi-main" in seen[-1]["detail"]



def test_run_manager_emits_provider_diagnostics_for_runtime_failures(tmp_path) -> None:
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    seen: list[tuple[EventType, dict]] = []

    async def go() -> None:
        async def sink(ev) -> None:
            if ev.event_type in (EventType.PROVIDER_ERROR, EventType.PROVIDER_BATCH_ALERT, EventType.RUN_FINISHED):
                seen.append((ev.event_type, ev.payload))

        async def driver(run) -> None:
            run.bus.add_sink(sink)
            raise RuntimeError("402 insufficient balance: please recharge your account")

        run = await mgr.start("provider-failed-run", driver)
        await run.task

    asyncio.run(go())

    provider_errors = [payload for typ, payload in seen if typ is EventType.PROVIDER_ERROR]
    assert provider_errors
    assert provider_errors[-1]["category"] == "insufficient_quota"
    assert provider_errors[-1]["severity"] == "fatal"
    assert provider_errors[-1]["should_pause_dispatch"] is True
    assert any(typ is EventType.RUN_FINISHED for typ, _ in seen)


def test_run_manager_emits_provider_batch_alert_for_repeated_runtime_failures(tmp_path) -> None:
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    seen: list[tuple[EventType, dict]] = []

    async def go() -> None:
        async def sink(ev) -> None:
            if ev.event_type in (EventType.PROVIDER_ERROR, EventType.PROVIDER_BATCH_ALERT):
                seen.append((ev.event_type, ev.payload))

        async def driver(run) -> None:
            run.bus.add_sink(sink)
            raise RuntimeError("402 insufficient quota: account balance exhausted")

        runs = []
        for idx in range(3):
            run = await mgr.start(f"provider-batch-{idx}", driver)
            runs.append(run)
            await run.task

    asyncio.run(go())

    provider_errors = [payload for typ, payload in seen if typ is EventType.PROVIDER_ERROR]
    batch_alerts = [payload for typ, payload in seen if typ is EventType.PROVIDER_BATCH_ALERT]
    assert len(provider_errors) == 3
    assert batch_alerts
    alert = batch_alerts[-1]
    assert alert["category"] == "insufficient_quota"
    assert alert["count"] == 3
    assert alert["should_pause_dispatch"] is True
