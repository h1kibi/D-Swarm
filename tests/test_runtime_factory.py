from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dswarm.core.event_bus import EventBus
from dswarm.solver.runtime_factory import build_docker_runtime_context
from dswarm.solver.container_pool import RuntimePoolView
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimePolicy,
    RuntimeResourceSpec,
    RuntimeSnapshot,
    build_runtime_policy,
)
from dswarm.solver.runtime_snapshot import (
    DockerImageInspector,
    RuntimeSnapshotBuilder,
    RuntimeSnapshotBuildError,
    RuntimeSnapshotStore,
)
from dswarm.swarm.budget import ProfileBudgetGate


def _snapshot(
    run_id: str,
    policy: RuntimePolicy,
    profiles: list[dict[str, Any]],
) -> RuntimeSnapshot:
    pools = []
    for index, profile in enumerate(profiles):
        profile_id = str(profile.get("name") or profile.get("id"))
        pools.append(PoolSpec.with_computed_id(
            profile_id=profile_id,
            runtime_kind=str(profile.get("engine") or "pi"),
            resolved_image_id="sha256:" + str(index + 1) * 64,
            requested_image_ref=str(profile.get("image") or "worker:test"),
            network=RuntimeNetworkSpec(kind="named", name="dswarm_net"),
            resources=RuntimeResourceSpec(
                cpus="1",
                memory="1g",
                pids_limit=128,
                tmpfs_bytes=1024,
            ),
            credential_binding_id=str(profile.get("credential_account") or "pi-main"),
            provider_binding_id=str(profile.get("provider_ref") or "deepseek"),
            model=str(profile.get("model") or "deepseek-chat"),
            uid=1000,
            gid=1000,
            runtime_features=("rcp-v2",),
            protocol_version=2,
            pool_max_concurrent_workers=2,
        ))
    return RuntimeSnapshot(
        version=1,
        run_id=run_id,
        created_at=1.0,
        runtime_policy=policy,
        shared_uid=1000,
        shared_gid=1000,
        pools=tuple(sorted(pools, key=lambda pool: (pool.profile_id, pool.pool_id))),
    )


class FakeDocker:
    def __init__(self):
        self.images: dict[str, str] = {}
        self.identity: tuple[int, int] = (1000, 1000)

    def resolve_image(self, ref):
        image_id = self.images.get(ref)
        return {"image_id": image_id} if image_id else None

    def query_user(self, image_id, user, *, network, mounts, env):
        return self.identity


class RecordingBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> RuntimeSnapshot:
        self.calls.append(dict(kwargs))
        return _snapshot(
            kwargs["run_id"],
            kwargs["policy"],
            list(kwargs["worker_profiles"]),
        )


class ExplodingBuilder:
    def build(self, **_kwargs: Any) -> RuntimeSnapshot:
        raise AssertionError("frozen snapshot must be loaded, not rebuilt")


def _profiles() -> list[dict[str, Any]]:
    return [
        {
            "id": "pi-web",
            "name": "pi-web",
            "engine": "pi",
            "runtime": "docker-web",
            "image": "worker:web",
            "credential_account": "pi-web-main",
            "base_url": "https://api.example/v1",
            "enabled": True,
        },
        {
            "id": "pi-worker",
            "name": "pi-worker",
            "engine": "pi",
            "runtime": "docker-web",
            "image": "worker:generic",
            "credential_account": "pi-main",
            "enabled": True,
        },
    ]


def _runtimes() -> list[dict[str, Any]]:
    return [{
        "id": "docker-web",
        "backend": "container",
        "network": "named",
        "network_name": "dswarm_net",
    }]


def test_runtime_context_builds_once_then_reloads_frozen_snapshot(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    store = RuntimeSnapshotStore(sessions)
    builder = RecordingBuilder()
    manager_calls: list[dict[str, Any]] = []

    def manager_factory(**kwargs: Any) -> object:
        manager_calls.append(dict(kwargs))
        return type("Manager", (), {
            "run_id": kwargs["run_id"],
            "snapshot": kwargs["snapshot"],
        })()

    first = build_docker_runtime_context(
        run_id="tui-run",
        sessions_root=sessions,
        bus=EventBus(),
        budget_gate=ProfileBudgetGate(),
        worker_profiles=_profiles(),
        runtime_profiles=_runtimes(),
        run_max_workers=2,
        snapshot_builder=builder,
        snapshot_store=store,
        pool_manager_factory=manager_factory,
    )

    assert len(builder.calls) == 1
    assert store.path_for("tui-run").is_file()
    assert first["worker_backend"] == "container"
    assert first["runtime_policy"] is first["runtime_snapshot"].runtime_policy
    assert first["pool_manager"].snapshot is first["runtime_snapshot"]
    assert first["pool_manager"].run_id == "tui-run"

    second = build_docker_runtime_context(
        run_id="tui-run",
        sessions_root=sessions,
        bus=EventBus(),
        budget_gate=ProfileBudgetGate(),
        worker_profiles=_profiles(),
        runtime_profiles=_runtimes(),
        run_max_workers=2,
        snapshot_builder=ExplodingBuilder(),
        snapshot_store=store,
        pool_manager_factory=manager_factory,
    )

    assert second["runtime_snapshot"].pools == first["runtime_snapshot"].pools
    assert second["pool_manager"].snapshot is second["runtime_snapshot"]
    assert len(manager_calls) == 2


def test_runtime_context_passes_direct_and_gateway_credential_modes(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def manager_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return type("Manager", (), {
            "run_id": kwargs["run_id"],
            "snapshot": kwargs["snapshot"],
        })()

    context = build_docker_runtime_context(
        run_id="tui-run",
        sessions_root=tmp_path / "sessions",
        bus=EventBus(),
        budget_gate=ProfileBudgetGate(),
        worker_profiles=_profiles(),
        runtime_profiles=_runtimes(),
        run_max_workers=2,
        snapshot_builder=RecordingBuilder(),
        pool_manager_factory=manager_factory,
    )

    modes = captured["credential_modes"]
    by_profile = {
        pool.profile_id: modes[pool.pool_id]
        for pool in context["runtime_snapshot"].pools
    }
    assert by_profile == {"pi-web": "direct", "pi-worker": "gateway"}
    assert captured["credential_projector"] is not None
    assert captured["probe"] is not None
    assert callable(captured["executor_factory"])


def test_provider_bound_profile_projects_gateway_mode(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def manager_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return type("Manager", (), {
            "run_id": kwargs["run_id"],
            "snapshot": kwargs["snapshot"],
        })()

    profile = {
        "id": "pi-glm",
        "name": "pi-glm",
        "engine": "pi",
        "runtime": "docker-web",
        "image": "worker:web",
        # normalize_worker_profile clears credential_account when a provider_ref
        # is set; the provider relay owns the secret, workers get task tokens.
        "credential_account": "",
        "provider_ref": "zhipu",
        "model": "glm-5.3-flash",
        "enabled": True,
    }
    runtime = {
        "id": "docker-web",
        "backend": "container",
        "network": "bridge",
        "cpus": "2.0",
        "memory": "2G",
        "pids_limit": 256,
        "tmpfs_bytes": 67108864,
    }
    docker = FakeDocker()
    docker.images["worker:web"] = "sha256:" + "a" * 64
    docker.identity = (1000, 1000)
    context = build_docker_runtime_context(
        run_id="tui-run",
        sessions_root=tmp_path / "sessions",
        bus=EventBus(),
        budget_gate=ProfileBudgetGate(),
        worker_profiles=[profile],
        runtime_profiles=[runtime],
        run_max_workers=2,
        snapshot_builder=RuntimeSnapshotBuilder(
            DockerImageInspector(docker, allow_pull=False),
        ),
        pool_manager_factory=manager_factory,
    )

    pool = context["runtime_snapshot"].pools[0]
    assert pool.credential_binding_id == "zhipu"
    assert captured["credential_modes"][pool.pool_id] == "gateway"


def test_runtime_context_wires_transition_callback_to_private_diagnostics(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def manager_factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return type("Manager", (), {
            "run_id": kwargs["run_id"],
            "snapshot": kwargs["snapshot"],
        })()

    build_docker_runtime_context(
        run_id="tui-run",
        sessions_root=tmp_path / "sessions",
        bus=EventBus(),
        budget_gate=ProfileBudgetGate(),
        worker_profiles=_profiles(),
        runtime_profiles=_runtimes(),
        run_max_workers=2,
        snapshot_builder=RecordingBuilder(),
        pool_manager_factory=manager_factory,
    )

    callback = captured["transition_callback"]
    callback(
        RuntimePoolView(
            pool_id="pool-a",
            state="ready",
            generation=1,
            pool_instance_id="instance-a",
            active_workers=0,
            waiting_workers=0,
            capacity=1,
            failure=None,
            recovery_episode=0,
        ),
        None,
    )

    lifecycle = (
        tmp_path
        / "sessions"
        / "tui-run"
        / ".runtime"
        / "pools"
        / "pool-a"
        / "diagnostics"
        / "lifecycle.jsonl"
    )
    assert lifecycle.is_file()
    assert '"state":"ready"' in lifecycle.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("profiles", "max_workers", "error"),
    [
        ([], 2, "no_worker_profiles"),
        (_profiles(), 0, "invalid_run_max_workers"),
    ],
)
def test_runtime_context_fails_closed_without_usable_capacity(
    tmp_path: Path,
    profiles: list[dict[str, Any]],
    max_workers: int,
    error: str,
) -> None:
    with pytest.raises(RuntimeSnapshotBuildError, match=error):
        build_docker_runtime_context(
            run_id="tui-run",
            sessions_root=tmp_path / "sessions",
            bus=EventBus(),
            budget_gate=ProfileBudgetGate(),
            worker_profiles=profiles,
            runtime_profiles=_runtimes(),
            run_max_workers=max_workers,
            snapshot_builder=RecordingBuilder(),
            pool_manager_factory=lambda **_kwargs: object(),
        )
