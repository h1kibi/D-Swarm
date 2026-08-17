from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from dswarm.solver.container_pool import (
    ContainerPoolManager,
    PoolCloseReport,
    RuntimeFailure,
    RuntimePoolView,
    RuntimeProbeResult,
    WorkerRuntimeLease,
)
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimeResourceSpec,
    RuntimeSnapshot,
    build_runtime_policy,
)


def make_pool(*, profile_id: str = "pi-web", capacity: int = 2) -> PoolSpec:
    return PoolSpec.with_computed_id(
        profile_id=profile_id,
        runtime_kind="pi",
        resolved_image_id="sha256:" + "a" * 64,
        requested_image_ref="dswarm/pi-web:test",
        network=RuntimeNetworkSpec(kind="none"),
        resources=RuntimeResourceSpec(
            cpus="1", memory="1g", pids_limit=128, tmpfs_bytes=16777216
        ),
        credential_binding_id="pi-web-main",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2", "tool-disabled-probe"),
        protocol_version=2,
        pool_max_concurrent_workers=capacity,
    )


def make_snapshot(*pools: PoolSpec) -> RuntimeSnapshot:
    selected = pools or (make_pool(),)
    return RuntimeSnapshot(
        version=1,
        run_id="run-a",
        created_at=1.0,
        runtime_policy=build_runtime_policy(env={}, max_pools_per_run=8),
        shared_uid=1000,
        shared_gid=1000,
        pools=tuple(sorted(selected, key=lambda pool: (pool.profile_id, pool.pool_id))),
    )


class UnusedFactory:
    async def __call__(self, **_kwargs):
        raise AssertionError("executor factory must not be called")


class UnusedProbe:
    async def run(self, **_kwargs):
        raise AssertionError("probe must not be called")


class UnusedProjector:
    def project(self, **_kwargs):
        raise AssertionError("credential projector must not be called")


class FakeProjection:
    def __init__(self) -> None:
        self.env = {"TOKEN": "secret"}
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_runtime_failure_is_frozen_and_exposes_only_machine_safe_fields():
    failure = RuntimeFailure(category="auth", code="credential_unavailable")

    assert str(failure) == "credential_unavailable"
    assert failure.snapshot() == {
        "category": "auth",
        "code": "credential_unavailable",
    }
    with pytest.raises(FrozenInstanceError):
        failure.code = "changed"
    with pytest.raises(ValueError, match="invalid_failure_code"):
        RuntimeFailure(category="auth", code="secret: /home/user/.pi/API_KEY")


@pytest.mark.asyncio
async def test_worker_runtime_lease_release_is_idempotent_and_hides_mutable_env():
    projection = FakeProjection()
    release_calls = 0

    async def release_once() -> None:
        nonlocal release_calls
        release_calls += 1
        projection.close()

    lease = WorkerRuntimeLease(
        pool_id="pool-v1::" + "a" * 40,
        pool_instance_id="instance-a",
        generation=1,
        worker_instance_id="worker-a",
        executor=SimpleNamespace(),
        credential_projection=projection,
        worker_env={"TOKEN": "secret"},
        _release_once=release_once,
    )

    projection.env["TOKEN"] = "changed"
    assert lease.worker_env == {"TOKEN": "secret"}
    with pytest.raises(TypeError):
        lease.worker_env["NEW"] = "value"

    await lease.release()
    await lease.release()
    assert release_calls == 1
    assert projection.close_calls == 1


@pytest.mark.asyncio
async def test_acquire_rejects_pool_not_present_in_frozen_snapshot():
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(),
        executor_factory=UnusedFactory(),
        probe=UnusedProbe(),
        credential_projector=UnusedProjector(),
    )

    with pytest.raises(RuntimeFailure) as raised:
        await manager.acquire(
            pool_id="pool-v1::" + "f" * 40,
            worker_instance_id="worker-a",
            operation_kind="worker",
        )

    assert raised.value.snapshot() == {
        "category": "configuration",
        "code": "unknown_pool",
    }
class FakeExecutor:
    def __init__(self, *, pool_id: str, generation: int) -> None:
        self.pool_id = pool_id
        self.generation = generation
        self.pool_instance_id = f"instance-{generation}"
        self.terminate_calls = 0

    async def terminate(self, *, require_proof: bool = False):
        assert require_proof is True
        self.terminate_calls += 1
        return SimpleNamespace(proof_complete=True)


class FakeExecutorFactory:
    def __init__(self) -> None:
        self.create_count = 0
        self.executor = None

    async def __call__(self, *, run_id, pool_spec, generation):
        assert run_id == "run-a"
        self.create_count += 1
        await asyncio.sleep(0)
        self.executor = FakeExecutor(pool_id=pool_spec.pool_id, generation=generation)
        return self.executor


class FakeProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self, *, executor, pool_spec, credential_projection, generation, timeout
    ):
        self.calls += 1
        assert executor.pool_id == pool_spec.pool_id
        assert generation == 1
        assert timeout == 45.0
        assert credential_projection.closed is False
        await asyncio.sleep(0)
        return RuntimeProbeResult(
            ready=True,
            probe_id="probe-1",
            failure=None,
            cache_identity="cache-1",
        )


class FakeCredentialProjection:
    def __init__(self, worker_instance_id: str) -> None:
        self.worker_instance_id = worker_instance_id
        self.env = {"DSWARM_WORKER": worker_instance_id}
        self.closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeProjector:
    def __init__(self) -> None:
        self.calls = []
        self.projections = []

    def project(self, **kwargs):
        self.calls.append(dict(kwargs))
        projection = FakeCredentialProjection(kwargs["worker_instance_id"])
        self.projections.append(projection)
        return projection


@pytest.mark.asyncio
async def test_concurrent_first_acquire_singleflights_create_and_probe():
    pool = make_pool(capacity=4)
    factory = FakeExecutorFactory()
    probe = FakeProbe()
    projector = FakeProjector()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=probe,
        credential_projector=projector,
    )

    leases = await asyncio.gather(
        *[
            manager.acquire(
                pool_id=pool.pool_id,
                worker_instance_id=f"worker-{index}",
                operation_kind="worker",
            )
            for index in range(4)
        ]
    )

    assert factory.create_count == 1
    assert probe.calls == 1
    assert {lease.pool_instance_id for lease in leases} == {"instance-1"}
    assert [lease.worker_env["DSWARM_WORKER"] for lease in leases] == [
        "worker-0",
        "worker-1",
        "worker-2",
        "worker-3",
    ]
    assert len(projector.calls) == 5
    assert projector.calls[0]["worker_instance_id"].startswith("probe-")
    assert projector.projections[0].closed is True

    await asyncio.gather(*(lease.release() for lease in leases))
    assert all(projection.closed for projection in projector.projections)
@pytest.mark.asyncio
async def test_cancelled_capacity_waiter_does_not_leak_permit():
    pool = make_pool(capacity=1)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=FakeExecutorFactory(),
        probe=FakeProbe(),
        credential_projector=FakeProjector(),
    )
    first = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )
    waiter = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-2",
            operation_kind="worker",
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await first.release()
    third = await asyncio.wait_for(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-3",
            operation_kind="worker",
        ),
        timeout=1,
    )
    await third.release()
class GatedExecutorFactory(FakeExecutorFactory):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *, run_id, pool_spec, generation):
        self.create_count += 1
        self.entered.set()
        await self.release.wait()
        self.executor = FakeExecutor(pool_id=pool_spec.pool_id, generation=generation)
        return self.executor


class GatedProbe(FakeProbe):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return RuntimeProbeResult(
            ready=True,
            probe_id="probe-gated",
            failure=None,
            cache_identity="cache-gated",
        )


def only_view(manager: ContainerPoolManager) -> RuntimePoolView:
    views = manager.snapshot_view()
    assert len(views) == 1
    return views[0]


@pytest.mark.asyncio
async def test_pool_state_progresses_starting_probing_ready_and_stopped():
    pool = make_pool(capacity=2)
    factory = GatedExecutorFactory()
    probe = GatedProbe()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=probe,
        credential_projector=FakeProjector(),
    )

    acquire = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-a",
            operation_kind="worker",
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)
    assert only_view(manager).state == "starting"

    factory.release.set()
    await asyncio.wait_for(probe.entered.wait(), timeout=1)
    assert only_view(manager).state == "probing"

    probe.release.set()
    lease = await asyncio.wait_for(acquire, timeout=1)
    ready = only_view(manager)
    assert ready.state == "ready"
    assert ready.generation == 1
    assert ready.pool_instance_id == "instance-1"
    assert ready.active_workers == 1
    assert ready.capacity == 2

    await lease.release()
    report = await manager.close()
    assert isinstance(report, PoolCloseReport)
    assert report.closed is True
    assert report.failures == ()
    assert only_view(manager).state == "stopped"
    assert factory.executor.terminate_calls == 1


@pytest.mark.asyncio
async def test_close_wakes_capacity_waiter_and_rejects_future_acquire():
    pool = make_pool(capacity=1)
    factory = FakeExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=FakeProbe(),
        credential_projector=FakeProjector(),
    )
    first = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )
    waiter = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-2",
            operation_kind="worker",
        )
    )
    await asyncio.sleep(0)

    first_report, second_report = await asyncio.gather(manager.close(), manager.close())
    assert first_report == second_report
    with pytest.raises(RuntimeFailure) as waiting_failure:
        await waiter
    assert waiting_failure.value.code == "manager_closed"
    assert first.credential_projection.closed is True

    with pytest.raises(RuntimeFailure) as future_failure:
        await manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-3",
            operation_kind="worker",
        )
    assert future_failure.value.snapshot() == {
        "category": "infrastructure",
        "code": "manager_closed",
    }
class MultiExecutorFactory:
    def __init__(self, *, cleanup_proven: bool = True) -> None:
        self.create_count = 0
        self.executors = []
        self.cleanup_proven = cleanup_proven

    async def __call__(self, *, run_id, pool_spec, generation):
        self.create_count += 1
        executor = FakeExecutor(pool_id=pool_spec.pool_id, generation=generation)
        executor.pool_instance_id = f"{pool_spec.profile_id}-instance-{generation}"
        executor.cleanup_proven = self.cleanup_proven

        async def terminate(*, require_proof=False):
            assert require_proof is True
            executor.terminate_calls += 1
            if not executor.cleanup_proven:
                from dswarm.solver.container_runtime import ContainerRuntimeError

                raise ContainerRuntimeError("cleanup_unproven")
            return SimpleNamespace(proof_complete=True)

        executor.terminate = terminate
        self.executors.append(executor)
        return executor


class SelectiveProbe:
    def __init__(self, bad_pool_id: str = "") -> None:
        self.bad_pool_id = bad_pool_id
        self.calls = 0

    async def run(self, *, executor, pool_spec, **_kwargs):
        self.calls += 1
        if pool_spec.pool_id == self.bad_pool_id:
            return RuntimeProbeResult(
                ready=False,
                probe_id="probe-bad",
                failure=RuntimeFailure(category="auth", code="probe_denied"),
                cache_identity="",
            )
        return RuntimeProbeResult(
            ready=True,
            probe_id="probe-ok",
            failure=None,
            cache_identity=f"cache-{pool_spec.profile_id}",
        )


@pytest.mark.asyncio
async def test_probe_failure_degrades_only_the_affected_pool():
    bad_pool = make_pool(profile_id="pi-bad", capacity=1)
    good_pool = make_pool(profile_id="pi-good", capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(bad_pool, good_pool),
        executor_factory=factory,
        probe=SelectiveProbe(bad_pool.pool_id),
        credential_projector=FakeProjector(),
    )

    bad_acquire, good_acquire = await asyncio.gather(
        manager.acquire(
            pool_id=bad_pool.pool_id,
            worker_instance_id="bad-worker",
            operation_kind="worker",
        ),
        manager.acquire(
            pool_id=good_pool.pool_id,
            worker_instance_id="good-worker",
            operation_kind="worker",
        ),
        return_exceptions=True,
    )

    assert isinstance(bad_acquire, RuntimeFailure)
    assert bad_acquire.snapshot() == {"category": "auth", "code": "probe_denied"}
    assert isinstance(good_acquire, WorkerRuntimeLease)
    views = {view.pool_id: view for view in manager.snapshot_view()}
    assert views[bad_pool.pool_id].state == "degraded"
    assert views[good_pool.pool_id].state == "ready"
    await good_acquire.release()
    await manager.close()


@pytest.mark.asyncio
async def test_marking_non_infrastructure_failure_degrades_pool_and_wakes_waiter():
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    first = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )
    waiter = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-2",
            operation_kind="worker",
        )
    )
    await asyncio.sleep(0)

    changed = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=first.pool_instance_id,
        failure=RuntimeFailure(category="auth", code="credential_revoked"),
    )

    assert changed is True
    with pytest.raises(RuntimeFailure) as raised:
        await waiter
    assert raised.value.snapshot() == {
        "category": "auth",
        "code": "credential_revoked",
    }
    assert factory.create_count == 1
    assert only_view(manager).state == "degraded"
    await first.release()
    await manager.close()


@pytest.mark.asyncio
async def test_concurrent_infrastructure_failure_singleflights_one_replacement():
    pool = make_pool(capacity=2)
    factory = MultiExecutorFactory()
    probe = SelectiveProbe()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=probe,
        credential_projector=FakeProjector(),
    )
    first = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )
    old_instance = first.pool_instance_id
    await first.release()
    failure = RuntimeFailure(category="infrastructure", code="runtime_link_lost")

    changed = await asyncio.gather(
        *[
            manager.mark_failure(
                pool_id=pool.pool_id,
                pool_instance_id=old_instance,
                failure=failure,
            )
            for _ in range(4)
        ]
    )

    assert changed == [True, True, True, True]
    assert factory.create_count == 2
    assert probe.calls == 2
    view = only_view(manager)
    assert view.state == "ready"
    assert view.generation == 2
    assert view.recovery_episode == 1
    assert factory.executors[0].terminate_calls == 1
    replacement = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-2",
        operation_kind="worker",
    )
    assert replacement.generation == 2
    assert replacement.pool_instance_id != old_instance
    await replacement.release()
    await manager.close()


@pytest.mark.asyncio
async def test_unproven_cleanup_blocks_replacement_generation():
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory(cleanup_proven=False)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )
    await lease.release()

    changed = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=lease.pool_instance_id,
        failure=RuntimeFailure(category="infrastructure", code="runtime_link_lost"),
    )

    assert changed is True
    assert factory.create_count == 1
    view = only_view(manager)
    assert view.state == "degraded"
    assert view.failure.code == "cleanup_unproven"
    await manager.close()
@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("new", "starting"),
        ("new", "degraded"),
        ("new", "stopping"),
        ("starting", "probing"),
        ("starting", "degraded"),
        ("starting", "stopping"),
        ("probing", "ready"),
        ("probing", "degraded"),
        ("probing", "stopping"),
        ("ready", "recovering"),
        ("ready", "degraded"),
        ("ready", "stopping"),
        ("recovering", "starting"),
        ("recovering", "degraded"),
        ("recovering", "stopping"),
        ("degraded", "stopping"),
        ("stopping", "stopped"),
    ],
)
def test_state_machine_accepts_each_legal_transition(current, target):
    entry = SimpleNamespace(state=current)
    ContainerPoolManager._transition(entry, target)
    assert entry.state == target


def test_transition_callback_failure_isolated_from_pool_state():
    pool = make_pool()

    def broken_callback(_view, _error):
        raise RuntimeError("diagnostics unavailable")

    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=UnusedFactory(),
        probe=UnusedProbe(),
        credential_projector=UnusedProjector(),
        transition_callback=broken_callback,
    )

    entry = manager._entries[pool.pool_id]
    manager._apply_transition(entry, "starting")

    assert entry.state == "starting"
    assert manager.transition_count == 1


def test_state_machine_rejects_illegal_transition_and_allows_idempotent_noop():
    entry = SimpleNamespace(state="ready")
    ContainerPoolManager._transition(entry, "ready")
    assert entry.state == "ready"
    with pytest.raises(RuntimeFailure) as raised:
        ContainerPoolManager._transition(entry, "probing")
    assert raised.value.code == "invalid_pool_transition"


@pytest.mark.asyncio
async def test_close_during_startup_returns_structured_failure_to_acquirer():
    pool = make_pool(capacity=1)
    factory = GatedExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=FakeProbe(),
        credential_projector=FakeProjector(),
    )
    acquire = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-a",
            operation_kind="worker",
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    report = await manager.close()

    assert report.closed is True
    with pytest.raises(RuntimeFailure) as raised:
        await acquire
    assert raised.value.code == "manager_closed"
    assert only_view(manager).state == "stopped"


@pytest.mark.asyncio
async def test_close_during_probe_terminates_created_generation_once():
    pool = make_pool(capacity=1)
    factory = GatedExecutorFactory()
    probe = GatedProbe()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=probe,
        credential_projector=FakeProjector(),
    )
    acquire = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-a",
            operation_kind="worker",
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)
    factory.release.set()
    await asyncio.wait_for(probe.entered.wait(), timeout=1)

    report = await manager.close()

    assert report.closed is True
    with pytest.raises(RuntimeFailure) as raised:
        await acquire
    assert raised.value.code == "manager_closed"
    assert factory.executor.terminate_calls == 1
    assert only_view(manager).state == "stopped"


@pytest.mark.parametrize("category", ["worker", "capacity"])
@pytest.mark.asyncio
async def test_worker_and_capacity_failures_do_not_degrade_pool(category):
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="worker",
    )

    changed = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=lease.pool_instance_id,
        failure=RuntimeFailure(category=category, code=f"{category}_failed"),
    )

    assert changed is False
    assert only_view(manager).state == "ready"
    await lease.release()
    await manager.close()


@pytest.mark.parametrize("category", ["identity", "auth", "configuration"])
@pytest.mark.asyncio
async def test_nonrecoverable_pool_failures_never_create_replacement(category):
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="worker",
    )

    changed = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=lease.pool_instance_id,
        failure=RuntimeFailure(category=category, code=f"{category}_failed"),
    )

    assert changed is True
    assert factory.create_count == 1
    assert only_view(manager).state == "degraded"
    await lease.release()
    await manager.close()


@pytest.mark.asyncio
async def test_stale_old_generation_failure_cannot_degrade_replacement():
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="worker",
    )
    old_instance = lease.pool_instance_id
    await lease.release()
    await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=old_instance,
        failure=RuntimeFailure(category="infrastructure", code="runtime_link_lost"),
    )

    changed = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=old_instance,
        failure=RuntimeFailure(category="auth", code="late_auth_failure"),
    )

    assert changed is False
    view = only_view(manager)
    assert view.state == "ready"
    assert view.generation == 2
    await manager.close()


class FailSecondCreateFactory(MultiExecutorFactory):
    async def __call__(self, *, run_id, pool_spec, generation):
        if generation == 2:
            self.create_count += 1
            from dswarm.solver.container_runtime import ContainerRuntimeError

            raise ContainerRuntimeError("runtime_create_failed")
        return await super().__call__(
            run_id=run_id, pool_spec=pool_spec, generation=generation
        )


@pytest.mark.asyncio
async def test_failed_recovery_attempt_is_not_retried_in_same_episode():
    pool = make_pool(capacity=1)
    factory = FailSecondCreateFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="worker",
    )
    old_instance = lease.pool_instance_id
    await lease.release()

    first = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=old_instance,
        failure=RuntimeFailure(category="infrastructure", code="runtime_link_lost"),
    )
    second = await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=old_instance,
        failure=RuntimeFailure(category="infrastructure", code="runtime_link_lost"),
    )

    assert first is True
    assert second is False
    assert factory.create_count == 2
    view = only_view(manager)
    assert view.state == "degraded"
    assert view.failure.code == "runtime_create_failed"
    await manager.close()
class GateSecondAcquireLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.calls = 0
        self.second_entered = asyncio.Event()
        self.release_second = asyncio.Event()

    async def __aenter__(self):
        self.calls += 1
        if self.calls == 2:
            self.second_entered.set()
            await self.release_second.wait()
        await self._lock.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self._lock.release()


@pytest.mark.asyncio
async def test_cancellation_after_capacity_grant_releases_the_permit():
    pool = make_pool(capacity=1)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=FakeExecutorFactory(),
        probe=FakeProbe(),
        credential_projector=FakeProjector(),
    )
    gate_lock = GateSecondAcquireLock()
    manager._entries[pool.pool_id].lock = gate_lock
    acquire = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-cancelled",
            operation_kind="worker",
        )
    )
    await asyncio.wait_for(gate_lock.second_entered.wait(), timeout=1)
    acquire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire

    replacement = await asyncio.wait_for(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-replacement",
            operation_kind="worker",
        ),
        timeout=1,
    )
    await replacement.release()
    await manager.close()


@pytest.mark.asyncio
async def test_mark_failure_can_resolve_pool_from_unique_pool_instance_identity():
    pool = make_pool(capacity=1)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=MultiExecutorFactory(),
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="worker",
    )

    changed = await manager.mark_failure(
        pool_instance_id=lease.pool_instance_id,
        failure=RuntimeFailure(category="auth", code="credential_revoked"),
    )

    assert changed is True
    assert only_view(manager).state == "degraded"
    await lease.release()
    await manager.close()

def test_probe_result_rejects_internally_inconsistent_terminal_state():
    with pytest.raises(ValueError, match="invalid_probe_result"):
        RuntimeProbeResult(
            ready=True,
            probe_id="probe-1",
            failure=RuntimeFailure(category="auth", code="probe_denied"),
            cache_identity="cache-1",
        )
    with pytest.raises(ValueError, match="invalid_probe_result"):
        RuntimeProbeResult(
            ready=False,
            probe_id="probe-2",
            failure=None,
            cache_identity="",
        )


@pytest.mark.asyncio
async def test_probe_failure_with_unproven_cleanup_reports_cleanup_residual():
    pool = make_pool(capacity=1)
    factory = MultiExecutorFactory(cleanup_proven=False)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(pool.pool_id),
        credential_projector=FakeProjector(),
    )

    with pytest.raises(RuntimeFailure) as raised:
        await manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-a",
            operation_kind="worker",
        )

    assert raised.value.snapshot() == {
        "category": "infrastructure",
        "code": "cleanup_unproven",
    }
    view = only_view(manager)
    assert view.state == "degraded"
    assert view.failure.code == "cleanup_unproven"
    assert view.pool_instance_id == factory.executors[0].pool_instance_id
    report = await manager.close()
    assert [failure.code for failure in report.failures] == ["cleanup_unproven"]


def test_manager_defensively_rejects_snapshot_over_pool_cap():
    first = make_pool(profile_id="pi-first")
    second = make_pool(profile_id="pi-second")
    snapshot = make_snapshot(first, second)
    object.__setattr__(snapshot.runtime_policy, "max_pools_per_run", 1)

    with pytest.raises(RuntimeFailure) as raised:
        ContainerPoolManager(
            run_id="run-a",
            snapshot=snapshot,
            executor_factory=UnusedFactory(),
            probe=UnusedProbe(),
            credential_projector=UnusedProjector(),
        )

    assert raised.value.snapshot() == {
        "category": "configuration",
        "code": "max_pools_per_run_exceeded",
    }


@pytest.mark.asyncio
async def test_cancelled_startup_waiter_does_not_cancel_shared_startup():
    pool = make_pool(capacity=2)
    factory = GatedExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=FakeProbe(),
        credential_projector=FakeProjector(),
    )
    cancelled = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-cancelled",
            operation_kind="worker",
        )
    )
    survivor = asyncio.create_task(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-survivor",
            operation_kind="worker",
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    factory.release.set()

    lease = await asyncio.wait_for(survivor, timeout=1)
    assert factory.create_count == 1
    assert lease.generation == 1
    await lease.release()
    await manager.close()


class FailFirstWorkerProjection(FakeProjector):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def project(self, **kwargs):
        if not kwargs["worker_instance_id"].startswith("probe-") and not self.failed:
            from dswarm.solver.runtime_credentials import CredentialProjectionError

            self.failed = True
            raise CredentialProjectionError("credential_unavailable", "unavailable")
        return super().project(**kwargs)


@pytest.mark.asyncio
async def test_worker_projection_failure_releases_capacity_without_degrading_ready_pool():
    pool = make_pool(capacity=1)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=FakeExecutorFactory(),
        probe=FakeProbe(),
        credential_projector=FailFirstWorkerProjection(),
    )

    with pytest.raises(RuntimeFailure) as raised:
        await manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-fails",
            operation_kind="worker",
        )
    assert raised.value.snapshot() == {
        "category": "auth",
        "code": "credential_unavailable",
    }
    assert only_view(manager).state == "ready"

    replacement = await asyncio.wait_for(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-replacement",
            operation_kind="worker",
        ),
        timeout=1,
    )
    await replacement.release()
    await manager.close()


@pytest.mark.asyncio
async def test_close_reports_safe_failure_and_still_cleans_other_pool():
    bad_pool = make_pool(profile_id="pi-bad", capacity=1)
    good_pool = make_pool(profile_id="pi-good", capacity=1)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(bad_pool, good_pool),
        executor_factory=factory,
        probe=SelectiveProbe(),
        credential_projector=FakeProjector(),
    )
    bad_lease, good_lease = await asyncio.gather(
        manager.acquire(
            pool_id=bad_pool.pool_id,
            worker_instance_id="bad-worker",
            operation_kind="worker",
        ),
        manager.acquire(
            pool_id=good_pool.pool_id,
            worker_instance_id="good-worker",
            operation_kind="worker",
        ),
    )
    bad_executor = next(executor for executor in factory.executors if executor.pool_id == bad_pool.pool_id)
    good_executor = next(executor for executor in factory.executors if executor.pool_id == good_pool.pool_id)
    bad_executor.cleanup_proven = False

    report = await manager.close()

    assert bad_lease.released is True
    assert good_lease.released is True
    assert [failure.snapshot() for failure in report.failures] == [
        {"category": "infrastructure", "code": "cleanup_unproven"}
    ]
    assert bad_executor.terminate_calls == 1
    assert good_executor.terminate_calls == 1
    assert {view.state for view in manager.snapshot_view()} == {"stopped"}


@pytest.mark.asyncio
async def test_shared_probe_failure_reaches_all_startup_waiters_with_same_safe_code():
    pool = make_pool(capacity=2)
    factory = MultiExecutorFactory()
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=factory,
        probe=SelectiveProbe(pool.pool_id),
        credential_projector=FakeProjector(),
    )

    outcomes = await asyncio.gather(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-a",
            operation_kind="worker",
        ),
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-b",
            operation_kind="worker",
        ),
        return_exceptions=True,
    )

    assert [outcome.snapshot() for outcome in outcomes] == [
        {"category": "auth", "code": "probe_denied"},
        {"category": "auth", "code": "probe_denied"},
    ]
    assert factory.create_count == 1
    await manager.close()



class FailWorkerProjectionCleanup(FakeProjector):
    def project(self, **kwargs):
        projection = super().project(**kwargs)
        if not kwargs["worker_instance_id"].startswith("probe-"):
            def fail_close():
                projection.close_calls += 1
                from dswarm.solver.runtime_credentials import CredentialProjectionCleanupError

                raise CredentialProjectionCleanupError("cleanup_unproven", "unproven")

            projection.close = fail_close
        return projection


@pytest.mark.asyncio
async def test_projection_cleanup_failure_does_not_leak_worker_capacity():
    pool = make_pool(capacity=1)
    manager = ContainerPoolManager(
        run_id="run-a",
        snapshot=make_snapshot(pool),
        executor_factory=FakeExecutorFactory(),
        probe=FakeProbe(),
        credential_projector=FailWorkerProjectionCleanup(),
    )
    first = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-first",
        operation_kind="worker",
    )

    with pytest.raises(RuntimeFailure) as raised:
        await first.release()
    assert raised.value.snapshot() == {
        "category": "infrastructure",
        "code": "cleanup_unproven",
    }
    assert first.released is True
    assert only_view(manager).active_workers == 0

    replacement = await asyncio.wait_for(
        manager.acquire(
            pool_id=pool.pool_id,
            worker_instance_id="worker-replacement",
            operation_kind="worker",
        ),
        timeout=1,
    )
    with pytest.raises(RuntimeFailure):
        await replacement.release()
    await manager.close()
