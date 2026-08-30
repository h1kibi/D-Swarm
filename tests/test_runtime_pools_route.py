"""The deck's runtime observability endpoint: pools + policy + lifecycle history."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from dswarm.solver.container_pool import RuntimeFailure, RuntimePoolView
from dswarm.solver.runtime_diagnostics import RuntimeDiagnosticsStore

RUN_ID = "run-pools-route"


def _view(state: str = "ready", *, failure: RuntimeFailure | None = None) -> RuntimePoolView:
    return RuntimePoolView(
        pool_id="pool-v1::abc",
        state=state,
        generation=2,
        pool_instance_id="instance-9",
        active_workers=1,
        waiting_workers=0,
        capacity=3,
        failure=failure,
        recovery_episode=0,
    )


@contextmanager
def _client(tmp_path: Path, views):
    fake_pool_manager = SimpleNamespace(snapshot_view=lambda: views)

    mgr = RunManager(sessions_root=tmp_path / "sessions")
    run = mgr.create(RUN_ID)
    run.runtime_policy = SimpleNamespace(mode="docker")
    run.pool_manager = fake_pool_manager
    # a real diagnostics history on disk for the same run
    store = RuntimeDiagnosticsStore(
        run_root=tmp_path / "sessions" / RUN_ID, run_id=RUN_ID,
    )
    for state in ("starting", "probing", "ready"):
        store.record_transition(_view(state))
    store.record_transition(_view("degraded", failure=RuntimeFailure("auth", "probe_denied")))
    yield TestClient(create_app(mgr))


def test_runtime_pools_route_surfaces_policy_state_and_history(tmp_path):
    with _client(tmp_path, [_view()]) as client:
        resp = client.get(f"/api/runs/{RUN_ID}/runtime-pools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == RUN_ID
    assert body["policy_mode"] == "docker"
    (pool,) = body["pools"]
    assert pool["pool_id"] == "pool-v1__abc"
    assert pool["state"] == "ready"
    assert pool["capacity"] == 3
    history = pool["history"]
    assert [row["state"] for row in history] == [
        "starting", "probing", "ready", "degraded",
    ]
    last = history[-1]
    assert last["failure"] == {"category": "auth", "code": "probe_denied"}
    assert last["reason_code"] == "probe_denied"
    # allowlisted fields only
    assert set(last) <= {
        "state", "reason_code", "recovery_episode", "updated_at", "kind",
        "generation", "failure",
    }


def test_runtime_pools_route_history_is_bounded_and_tolerant(tmp_path):
    with _client(tmp_path, [_view()]) as client:
        resp = client.get(f"/api/runs/{RUN_ID}/runtime-pools")
    (pool,) = resp.json()["pools"]
    assert len(pool["history"]) <= 8


def test_runtime_pools_route_without_pool_manager(tmp_path):
    mgr = RunManager(sessions_root=tmp_path / "sessions")
    mgr.create(RUN_ID)
    client = TestClient(create_app(mgr))
    resp = client.get(f"/api/runs/{RUN_ID}/runtime-pools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pools"] == []
    assert body["policy_mode"] == ""
