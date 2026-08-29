"""E2E: the web launch route must freeze the M9a runtime context.

Regression for run-4408: a container-profile run dispatched from the web deck
silently died — every spawn raised ``runtime_policy_required`` because the
launch path never froze a runtime policy (only the TUI path did), while the
rail kept showing a live run and the conversation waited on skeletons. These
tests pin the wiring through the real HTTP route:

- container profiles -> docker policy frozen + snapshot persisted + pool
  manager composed from the RunManager factory, with the offline network
  clamp baked into the frozen spec;
- local profiles -> dual-gate honored (deny without DSWARM_ALLOW_LOCAL_WORKERS,
  local_dev policy with no snapshot when allowed);
- image-preflight failure fails the POST, not the run;
- freeze is idempotent across re-dispatch; mock drivers skip it entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from dswarm.solver.runtime_snapshot import ResolvedWorkerImage, RuntimeSnapshotBuilder

RUN_ID = "run-launch1"
IMAGE_REF = "swarm-test-image:1"

WORKER_PROFILES = [
    {
        "id": "seat_pi_web",
        "name": "pi-web",
        "label": "pi-web",
        "engine": "pi",
        "transport": "pi",
        "runtime": "docker-web",
        "image": IMAGE_REF,
        "enabled": True,
        "max_running": 2,
        "model": "deepseek-v4-pro",
        "credential_account": "pi-main",
        "api_key_ref": "",
        "provider_ref": "",
        "base_url": "",
    }
]
RUNTIME_PROFILES = [{"id": "docker-web", "backend": "container", "image": IMAGE_REF}]

LAUNCH_BODY = {
    "kind": "swarm",
    "challenge": {"name": "launch-e2e", "category": "pwn"},
    "prompt": "solve the launch e2e target",
    "worker_backend": "container",
    "engines": ["pi-web"],
    "worker_profiles": WORKER_PROFILES,
    "runtime_profiles": RUNTIME_PROFILES,
    "max_workers": 2,
}


class _StubImages:
    """Snapshot preflight without touching a Docker daemon.

    Mirrors DockerImageInspector.resolve's contract: an unresolvable ref raises
    the structured ``image_resolution_failed`` build error (the builder never
    receives None).
    """

    def __init__(self, *, resolve_ok: bool = True) -> None:
        self.resolve_ok = resolve_ok

    def resolve(self, ref: str):
        from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuildError

        if not self.resolve_ok:
            raise RuntimeSnapshotBuildError(
                "image_resolution_failed", "worker image is unavailable"
            )
        return ResolvedWorkerImage(
            requested_ref=ref, image_id=f"id-{ref}", uid=1000, gid=1000
        )

    def pull_image(self, ref: str) -> bool:
        return self.resolve_ok

    def query_user(self, *_args, **_kwargs):
        return (1000, 1000)


def make_client(tmp_path, *, resolve_ok: bool = True):
    pool_calls: list[dict] = []

    def pool_factory(**kwargs):
        pool_calls.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"], snapshot=kwargs["snapshot"])

    builder = RuntimeSnapshotBuilder(image_inspector=_StubImages(resolve_ok=resolve_ok))
    mgr = RunManager(
        sessions_root=tmp_path / "sessions",
        runtime_snapshot_builder=builder,
        runtime_pool_manager_factory=pool_factory,
    )
    launches: list[tuple[str, object]] = []

    def _captured_launch(self, run, driver):  # never executes drive()
        launches.append((run.run_id, driver))

    mgr._launch = _captured_launch.__get__(mgr)  # type: ignore[method-assign]
    app = create_app(mgr)
    return TestClient(app), mgr, pool_calls, launches


@pytest.fixture(autouse=True)
def _no_retention(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DSWARM_RETENTION_ENABLED", "0")


def _start(client: TestClient, run_id: str = RUN_ID, **overrides):
    body = {**LAUNCH_BODY, **overrides}
    return client.post(f"/api/runs/{run_id}/start", json=body)


def test_container_launch_freezes_docker_policy_snapshot_and_pool(tmp_path):
    client, mgr, pool_calls, launches = make_client(tmp_path)

    resp = _start(client)

    assert resp.status_code == 200, resp.text
    run = mgr.get(RUN_ID)
    assert run is not None and run.runtime_policy is not None
    assert run.runtime_policy.mode == "docker"
    # snapshot persisted create-once in the run-scoped store
    assert mgr.runtime_snapshot_store.path_for(RUN_ID).is_file()
    # pool manager composed through the manager factory with the frozen snapshot
    assert len(pool_calls) == 1
    assert pool_calls[0]["run_id"] == RUN_ID
    assert pool_calls[0]["snapshot"] is run.runtime_snapshot
    # network preserved from the runtime profile when the offline switch is
    # absent (clamp must NOT fire by default: network:none pools can never
    # pass the reverse-dial hello)
    assert run.runtime_snapshot.pools
    assert all(pool.network.kind == "bridge" for pool in run.runtime_snapshot.pools)
    # the dispatch path (drivers.runtime_context_kwargs) now sees frozen objects
    assert run.runtime_snapshot is not None and run.pool_manager is not None
    assert launches and launches[0][0] == RUN_ID


def test_local_backend_requires_dual_gate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    client, mgr, pool_calls, _ = make_client(tmp_path)
    monkeypatch.delenv("DSWARM_ALLOW_LOCAL_WORKERS", raising=False)

    resp = _start(client, RUN_ID, worker_backend="local")

    assert resp.status_code == 400
    assert "local_worker_policy_denied" in resp.json()["detail"]
    assert mgr.get(RUN_ID).runtime_policy is None
    assert pool_calls == []

    monkeypatch.setenv("DSWARM_ALLOW_LOCAL_WORKERS", "1")
    # the second gate: the launch itself must explicitly request local-dev
    resp = _start(client, RUN_ID, worker_backend="local")
    assert resp.status_code == 400
    assert "local_worker_policy_denied" in resp.json()["detail"]

    resp = _start(client, RUN_ID, worker_backend="local", local_dev=True)
    assert resp.status_code == 200, resp.text
    run = mgr.get(RUN_ID)
    assert run.runtime_policy is not None and run.runtime_policy.mode == "local_dev"
    assert run.runtime_snapshot is None and run.pool_manager is None
    assert pool_calls == []


def test_image_preflight_failure_fails_launch_not_the_run(tmp_path):
    client, mgr, pool_calls, _ = make_client(tmp_path, resolve_ok=False)

    resp = _start(client)

    assert resp.status_code == 400
    assert "image_resolution_failed" in resp.json()["detail"]
    run = mgr.get(RUN_ID)
    assert run.runtime_policy is None
    assert pool_calls == []


def test_freeze_is_idempotent_across_redispatch(tmp_path):
    client, mgr, pool_calls, _ = make_client(tmp_path)

    first = _start(client)
    policy_after_first = mgr.get(RUN_ID).runtime_policy
    second = _start(client, **{"max_workers": 3})

    assert first.status_code == 200 and second.status_code == 200
    assert mgr.get(RUN_ID).runtime_policy is policy_after_first
    assert len(pool_calls) == 1
    # exactly one snapshot artifact: create-once, later launches reload it
    snapshots = [
        p for p in (tmp_path / "sessions").rglob("*snapshot*") if p.is_file()
    ]
    assert len(snapshots) <= 1


def test_mock_driver_skips_runtime_freeze(tmp_path):
    client, mgr, pool_calls, _ = make_client(tmp_path)

    resp = _start(client, kind="mock")

    assert resp.status_code == 200, resp.text
    assert mgr.get(RUN_ID).runtime_policy is None
    assert pool_calls == []


def test_explicit_offline_clamps_container_network_to_none(tmp_path):
    client, _mgr, _pool_calls, _launches = make_client(tmp_path)

    resp = _start(client, RUN_ID + "-off", offline=True)

    assert resp.status_code == 200, resp.text
    mgr2 = _mgr
    run = mgr2.get(RUN_ID + "-off")
    assert run.runtime_snapshot is not None
    assert all(pool.network.kind == "none" for pool in run.runtime_snapshot.pools)
