from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx
import pytest

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from dswarm.solver.container_pool import RuntimeFailure, RuntimePoolView
from dswarm.solver.runtime_diagnostics import RuntimeDiagnosticsStore


def _view(pool_id: str = "pool-a") -> RuntimePoolView:
    return RuntimePoolView(
        pool_id=pool_id,
        state="ready",
        generation=2,
        pool_instance_id="instance-a",
        active_workers=1,
        waiting_workers=0,
        capacity=2,
        failure=None,
        recovery_episode=0,
    )


def test_private_state_and_jsonl_are_secret_free(tmp_path):
    store = RuntimeDiagnosticsStore(run_root=tmp_path, run_id="run-a")
    store.record_transition(_view(), error="Bearer secret-token at C:/Users/me/.pi")

    payload = json.loads(store.state_path("pool-a").read_text(encoding="utf-8"))
    line = json.loads(
        store.lifecycle_path("pool-a").read_text(encoding="utf-8").splitlines()[0]
    )
    serialized = json.dumps([payload, line])
    assert "secret-token" not in serialized
    assert "C:/Users/me" not in serialized
    assert payload["pool_id"] == "pool-a"
    assert payload["reason_code"] == "runtime_operation_failed"
    mode = store.state_path("pool-a").stat().st_mode & 0o777
    # Windows exposes ACL-backed mode bits rather than POSIX permissions; the
    # implementation still requests 0600 and the private-root contract applies.
    allowed_modes = {0o600, 0o644} if os.name != "nt" else {0o600, 0o644, 0o666}
    assert mode in allowed_modes


def test_pool_ids_are_sanitized_and_partial_tail_is_ignored(tmp_path):
    store = RuntimeDiagnosticsStore(run_root=tmp_path, run_id="run-a")
    store.record_transition(_view("pool-v1::abc/../../secret"))
    state = store.state_path("pool-v1::abc/../../secret")
    assert state.parent == store.root / "pool-v1__abc_____secret"
    assert state.is_file()
    lifecycle = store.lifecycle_path("pool-v1::abc/../../secret")
    lifecycle.open("a", encoding="utf-8").write('{"partial":')
    records = store.read_lifecycle("pool-v1::abc/../../secret")
    assert len(records) == 1


def test_transition_failure_is_typed_and_does_not_store_raw_error(tmp_path):
    store = RuntimeDiagnosticsStore(run_root=tmp_path, run_id="run-a")
    failed = RuntimePoolView(
        pool_id="pool-a",
        state="degraded",
        generation=3,
        pool_instance_id="instance-a",
        active_workers=0,
        waiting_workers=0,
        capacity=2,
        failure=RuntimeFailure(category="auth", code="credential_projection_failed"),
        recovery_episode=1,
    )
    record = store.record_transition(failed)
    assert record["failure"] == {
        "category": "auth",
        "code": "credential_projection_failed",
    }
    assert record["actor"] == ""


@pytest.mark.asyncio
async def test_runtime_pools_get_is_read_only(tmp_path):
    manager = RunManager(sessions_root=tmp_path / "sessions")
    run = manager.create("run-a")

    @dataclass
    class FakePoolManager:
        transition_count: int = 7

        def snapshot_view(self):
            return (_view(),)

    run.pool_manager = FakePoolManager()
    app = create_app(manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        trust_env=False,
    ) as client:
        response = await client.get("/api/runs/run-a/runtime-pools")
        missing = await client.get("/api/runs/missing/runtime-pools")
    assert response.status_code == 200
    body = response.json()
    # no runtime policy frozen for this bare run -> empty mode; each pool now
    # also carries its (empty here) sanitized lifecycle history
    assert body["run_id"] == "run-a"
    assert body["policy_mode"] == ""
    assert body["pools"] == [
        {
            "pool_id": "pool-a",
            "state": "ready",
            "generation": 2,
            "pool_instance_id": "instance-a",
            "active_workers": 1,
            "waiting_workers": 0,
            "capacity": 2,
            "failure": None,
            "recovery_episode": 0,
            "history": [],
        }
    ]
    assert missing.status_code == 404
    assert run.pool_manager.transition_count == 7
