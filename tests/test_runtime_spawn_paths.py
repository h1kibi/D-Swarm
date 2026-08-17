from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from dswarm.solver.runtime_policy import RuntimePolicyError
from dswarm.swarm import runtime as runtime_module


@pytest.mark.parametrize(
    "operation",
    [
        "bootstrap",
        "ordinary",
        "review",
        "recon",
        "recovery",
        "standby",
        "resolve",
        "btw",
    ],
)
def test_runtime_spawn_request_accepts_only_audited_operation_kinds(
    operation: str,
) -> None:
    request_type = getattr(runtime_module, "RuntimeSpawnRequest", None)
    assert request_type is not None, "RuntimeSpawnRequest must exist at the runtime boundary"

    request = request_type(
        profile_id="pi-main",
        worker_instance_id="worker-1",
        operation_kind=operation,
        mode="explore",
        intent_id="intent-1",
    )

    assert request.operation_kind == operation
    with pytest.raises(FrozenInstanceError):
        request.mode = "review"


def test_runtime_spawn_request_rejects_unknown_operation_kind() -> None:
    request_type = getattr(runtime_module, "RuntimeSpawnRequest", None)
    assert request_type is not None, "RuntimeSpawnRequest must exist at the runtime boundary"

    with pytest.raises(ValueError, match="invalid_runtime_operation_kind"):
        request_type(
            profile_id="pi-main",
            worker_instance_id="worker-1",
            operation_kind="web",
            mode="explore",
        )


class _RecordingManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.lease = object()

    async def acquire(self, **kwargs: str):
        self.calls.append(dict(kwargs))
        return self.lease


@pytest.mark.asyncio
async def test_runtime_lease_factory_uses_frozen_request_and_snapshot_pool() -> None:
    from types import SimpleNamespace

    factory_builder = getattr(runtime_module, "runtime_lease_factory_for_request", None)
    assert factory_builder is not None, "runtime boundary must own lease construction"
    snapshot = SimpleNamespace(
        pools=(SimpleNamespace(profile_id="pi-main", pool_id="pool-frozen"),)
    )
    manager = _RecordingManager()
    request = runtime_module.RuntimeSpawnRequest(
        profile_id="pi-main",
        worker_instance_id="worker-1",
        operation_kind="review",
        mode="review",
        intent_id="intent-1",
    )

    factory = factory_builder(snapshot=snapshot, pool_manager=manager, request=request)
    lease = await factory("worker-1", "review")

    assert lease is manager.lease
    assert manager.calls == [{
        "pool_id": "pool-frozen",
        "worker_instance_id": "worker-1",
        "operation_kind": "review",
    }]


@pytest.mark.asyncio
async def test_runtime_lease_factory_rejects_identity_or_operation_drift() -> None:
    from types import SimpleNamespace

    factory_builder = getattr(runtime_module, "runtime_lease_factory_for_request", None)
    assert factory_builder is not None, "runtime boundary must own lease construction"
    snapshot = SimpleNamespace(
        pools=(SimpleNamespace(profile_id="pi-main", pool_id="pool-frozen"),)
    )
    manager = _RecordingManager()
    request = runtime_module.RuntimeSpawnRequest(
        profile_id="pi-main",
        worker_instance_id="worker-1",
        operation_kind="ordinary",
        mode="explore",
    )
    factory = factory_builder(snapshot=snapshot, pool_manager=manager, request=request)

    with pytest.raises(ValueError, match="runtime_worker_identity_mismatch"):
        await factory("worker-2", "ordinary")
    with pytest.raises(ValueError, match="runtime_operation_kind_mismatch"):
        await factory("worker-1", "web")
    assert manager.calls == []


def test_runtime_lease_factory_rejects_profile_missing_from_snapshot() -> None:
    from types import SimpleNamespace

    factory_builder = getattr(runtime_module, "runtime_lease_factory_for_request", None)
    assert factory_builder is not None, "runtime boundary must own lease construction"
    snapshot = SimpleNamespace(
        pools=(SimpleNamespace(profile_id="other", pool_id="pool-other"),)
    )

    with pytest.raises(RuntimePolicyError, match="runtime_profile_not_in_snapshot"):
        factory_builder(
            snapshot=snapshot,
            pool_manager=_RecordingManager(),
            request=runtime_module.RuntimeSpawnRequest(
                profile_id="pi-main",
                worker_instance_id="worker-1",
                operation_kind="bootstrap",
                mode="bootstrap",
            ),
        )

@pytest.mark.parametrize(
    ("mode", "profile_role", "requested", "expected"),
    [
        ("bootstrap", "", "", "bootstrap"),
        ("explore", "", "", "ordinary"),
        ("review", "", "", "review"),
        ("bootstrap", "recon", "", "recon"),
        ("explore", "", "recovery", "recovery"),
        ("respond", "", "standby", "standby"),
        ("bootstrap", "", "resolve", "resolve"),
        ("btw", "", "btw", "btw"),
    ],
)
def test_runtime_operation_for_spawn_separates_audit_operation_from_mode(
    mode: str, profile_role: str, requested: str, expected: str,
) -> None:
    assert runtime_module.runtime_operation_for_spawn(
        mode=mode,
        profile_role=profile_role,
        requested=requested,
    ) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "worker_class", "requested", "expected"),
    [
        ("explore", "code", "", "ordinary"),
        ("recon", "code", "", "recon"),
        ("review", "review", "", "review"),
        ("explore", "code", "recovery", "recovery"),
    ],
)
async def test_swarm_worker_runtime_passes_audited_operation_to_worker_factory(
    mode: str,
    worker_class: str,
    requested: str,
    expected: str,
) -> None:
    from types import SimpleNamespace

    from dswarm.swarm.agents import AgentProfile, DispatchDecision

    seen: list[dict[str, object]] = []

    class _LaneGate:
        def lane_for(self, *, mode: str, worker_class: str) -> str:
            return "review" if mode == "review" or worker_class == "review" else "ordinary"

    class _Worker:
        async def run(self):
            return SimpleNamespace(solved=False)

    class _Swarm:
        challenge = SimpleNamespace(category="web")
        _worker_lane_gate = _LaneGate()
        shared_graph = None

        @staticmethod
        def _healthy_matches(engine: str, healthy: list[str]) -> bool:
            return engine in healthy

        @staticmethod
        def _make_cli_worker(engine: str, **kwargs: object):
            seen.append({"engine": engine, **kwargs})
            return _Worker()

        @staticmethod
        def _release_worker_account(worker: object) -> None:
            return None

        @staticmethod
        def _cancel_solver(worker: object) -> None:
            return None

    runtime = runtime_module.SwarmWorkerRuntime(_Swarm(), healthy=["pi-web"])
    await runtime.run(
        DispatchDecision(
            intent_id="I1",
            profile="pi-web",
            goal="inspect",
            mode=mode,
            worker_class=worker_class,
            runtime_operation_kind=requested,
        ),
        AgentProfile(id="pi-web", worker_profile="pi-web", mode=mode),
    )

    assert seen[0]["runtime_operation_kind"] == expected


def test_web_driver_runtime_context_kwargs_uses_run_frozen_objects() -> None:
    from types import SimpleNamespace

    from apps.web import drivers

    policy = object()
    snapshot = object()
    manager = object()
    run = SimpleNamespace(
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
    )

    assert drivers.runtime_context_kwargs(run) == {
        "runtime_policy": policy,
        "runtime_snapshot": snapshot,
        "pool_manager": manager,
    }

def test_btw_shell_path_does_not_own_legacy_containers() -> None:
    # Task 15 removes BTW's second run-global container. The remaining inert
    # Swarm compatibility facade is deleted by the later M9 invariant task.
    paths = ("dswarm/solver/btw.py", "apps/web/routes/btw.py")
    forbidden = ("ensure_container(", "_container_handle")

    for path in paths:
        source = Path(path).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path} still owns legacy runtime token {token}"
