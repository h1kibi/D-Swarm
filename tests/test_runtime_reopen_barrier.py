from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.web.run_manager import RunManager
from dswarm.solver.container_runtime import ContainerInspection, ContainerMount
from dswarm.solver.runtime_cleanup import (
    RuntimeCleanupExpectation,
    RuntimeCleanupInspector,
)


INSTANCE_A = "123e4567-e89b-42d3-a456-426614174000"
INSTANCE_B = "223e4567-e89b-42d3-a456-426614174000"


def _expected(*, pool_id: str, container_id: str, instance: str) -> RuntimeCleanupExpectation:
    mounts = (ContainerMount(f"/sessions/run-a/{pool_id}/workspace", "/home/kali/workspace", False),)
    return RuntimeCleanupExpectation(
        container_id=container_id,
        run_id="run-a",
        pool_id=pool_id,
        pool_instance_id=instance,
        generation=2,
        image_id="sha256:" + "a" * 64,
        network="bridge",
        mounts=mounts,
        private_state_mounts=mounts,
        worker_token_ids=(f"worker-{pool_id}",),
    )


def _inspection(expected: RuntimeCleanupExpectation) -> ContainerInspection:
    return ContainerInspection(
        container_id=expected.container_id,
        image_id=expected.image_id,
        labels=expected.labels,
        mounts=expected.mounts,
        network=expected.network,
        uid=1000,
        gid=1000,
        running=True,
    )


class _Docker:
    def __init__(self, inspections: dict[str, ContainerInspection], *, fail: set[str] | None = None):
        self.inspections = inspections
        self.fail = fail or set()
        self.removed: list[str] = []
        self.inspected: list[str] = []
        self.listed: list[dict[str, Any]] = []

    def inspect(self, container_id: str) -> ContainerInspection:
        self.inspected.append(container_id)
        if container_id in self.fail:
            raise RuntimeError("inspect failed")
        if container_id in self.removed:
            raise LookupError("no such container")
        return self.inspections[container_id]

    def remove(self, container_id: str, *, force: bool) -> bool:
        self.removed.append(container_id)
        return True


class _Receiver:
    def __init__(self) -> None:
        self.pools: list[str] = []
        self.tokens: list[str] = []

    def revoke_pool_instance(self, pool_instance_id: str) -> None:
        self.pools.append(pool_instance_id)

    def revoke_token(self, token_id: str) -> None:
        self.tokens.append(token_id)


@pytest.mark.parametrize("missing", [False, True])
def test_run_wide_barrier_cleans_every_exact_candidate(tmp_path: Path, missing: bool) -> None:
    first = _expected(pool_id="pool-a", container_id="container-a", instance=INSTANCE_A)
    second = _expected(pool_id="pool-b", container_id="container-b", instance=INSTANCE_B)
    docker = _Docker({first.container_id: _inspection(first), second.container_id: _inspection(second)})
    receiver = _Receiver()
    expectations = (first, second) if not missing else (first,)
    inspector = RuntimeCleanupInspector(
        docker=docker,
        receiver=receiver,
        candidate_provider=lambda run_id, run_root: expectations,
    )

    result = inspector.cleanup_run_before_reopen("run-a", tmp_path)

    assert result.proven is True
    expected_containers = {"container-a"} if missing else {"container-a", "container-b"}
    expected_instances = {INSTANCE_A} if missing else {INSTANCE_A, INSTANCE_B}
    assert set(docker.removed) == expected_containers
    assert set(receiver.pools) == expected_instances
    assert result.failures == ()


def test_barrier_attempts_all_pools_and_rejects_when_one_is_unproven(tmp_path: Path) -> None:
    first = _expected(pool_id="pool-a", container_id="container-a", instance=INSTANCE_A)
    second = _expected(pool_id="pool-b", container_id="container-b", instance=INSTANCE_B)
    docker = _Docker(
        {first.container_id: _inspection(first), second.container_id: _inspection(second)},
        fail={first.container_id},
    )
    receiver = _Receiver()
    inspector = RuntimeCleanupInspector(
        docker=docker,
        receiver=receiver,
        candidate_provider=lambda run_id, run_root: (first, second),
    )

    result = inspector.cleanup_run_before_reopen("run-a", tmp_path)

    assert result.proven is False
    assert set(docker.inspected) == {"container-a", "container-b"}
    assert "inspect_failed" in result.failures
    assert receiver.pools == [INSTANCE_A, INSTANCE_B]


def test_missing_cleanup_expectations_are_not_proof(tmp_path: Path) -> None:
    inspector = RuntimeCleanupInspector(docker=_Docker({}))

    result = inspector.cleanup_run_before_reopen("run-a", tmp_path)

    assert result.proven is False
    assert "cleanup_expectations_unavailable" in result.failures


def test_name_only_legacy_evidence_is_not_accepted(tmp_path: Path) -> None:
    inspector = RuntimeCleanupInspector(
        docker=_Docker({}),
        candidate_provider=lambda run_id, run_root: ("legacy-container-name",),
    )

    result = inspector.cleanup_run_before_reopen("run-a", tmp_path)

    assert result.proven is False
    assert "legacy_runtime_evidence_insufficient" in result.failures


@pytest.mark.asyncio
async def test_delete_closes_pool_manager_before_removing_run(tmp_path: Path) -> None:
    manager = RunManager(sessions_root=tmp_path)
    run = manager.create("run-a")
    calls: list[str] = []

    class PoolManager:
        async def close(self) -> None:
            calls.append("close")

    run.pool_manager = PoolManager()  # type: ignore[assignment]
    await manager.delete("run-a")

    assert calls == ["close"]


@pytest.mark.asyncio
async def test_reopen_barrier_failure_is_fail_closed(tmp_path: Path) -> None:
    calls: list[str] = []

    class Barrier:
        def cleanup_run_before_reopen(self, run_id: str, run_root: Path):
            calls.append("cleanup_barrier")
            return type("Result", (), {"proven": False, "failures": ("inspect_failed",)})()

    manager = RunManager(sessions_root=tmp_path, runtime_cleanup_inspector=Barrier())
    run = manager.create("run-a")

    with pytest.raises(RuntimeError, match="stale_runtime_cleanup_unproven"):
        await manager._cleanup_before_reopen(run)

    assert calls == ["cleanup_barrier"]


@pytest.mark.asyncio
async def test_shutdown_closes_each_pool_manager_and_continues_after_failure(tmp_path: Path) -> None:
    manager = RunManager(sessions_root=tmp_path)
    first = manager.create("run-a")
    second = manager.create("run-b")
    calls: list[str] = []

    class FailingPoolManager:
        async def close(self) -> None:
            calls.append("bad")
            raise RuntimeError("close_failed")

    class PoolManager:
        async def close(self) -> None:
            calls.append("good")

    first.pool_manager = FailingPoolManager()  # type: ignore[assignment]
    second.pool_manager = PoolManager()  # type: ignore[assignment]

    await manager.shutdown()

    assert calls == ["bad", "good"]
