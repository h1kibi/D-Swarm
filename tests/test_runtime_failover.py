"""M9a Task 16: frozen failover, pool-local isolation, and terminal criteria."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dswarm.solver.container_pool import (
    ContainerPoolManager,
    RuntimeFailure,
    RuntimePoolView,
    RuntimeProbeResult,
)
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimeResourceSpec,
    RuntimeSnapshot,
    build_runtime_policy,
)
from dswarm.swarm import runtime as runtime_module
from dswarm.swarm.agents import AgentProfile, DispatchDecision


def make_pool(profile_id: str, *, capacity: int = 2) -> PoolSpec:
    return PoolSpec.with_computed_id(
        profile_id=profile_id,
        runtime_kind="pi",
        resolved_image_id="sha256:" + profile_id.encode().hex().ljust(64, "a")[:64],
        requested_image_ref=f"dswarm/{profile_id}:test",
        network=RuntimeNetworkSpec(kind="none"),
        resources=RuntimeResourceSpec(
            cpus="1", memory="1g", pids_limit=128, tmpfs_bytes=16777216
        ),
        credential_binding_id=f"{profile_id}-account",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2", "tool-disabled-probe"),
        protocol_version=2,
        pool_max_concurrent_workers=capacity,
    )


def make_snapshot(*profile_ids: str) -> RuntimeSnapshot:
    pools = tuple(sorted((make_pool(profile) for profile in profile_ids), key=lambda p: (p.profile_id, p.pool_id)))
    return RuntimeSnapshot(
        version=1,
        run_id="run-failover",
        created_at=1.0,
        runtime_policy=build_runtime_policy(env={}, max_pools_per_run=16),
        shared_uid=1000,
        shared_gid=1000,
        pools=pools,
    )


def view(
    pool: PoolSpec,
    state: str,
    *,
    active_workers: int = 0,
    failure: RuntimeFailure | None = None,
    generation: int = 1,
) -> RuntimePoolView:
    return RuntimePoolView(
        pool_id=pool.pool_id,
        state=state,
        generation=generation,
        pool_instance_id=f"instance-{pool.profile_id}",
        active_workers=active_workers,
        waiting_workers=0,
        capacity=pool.pool_max_concurrent_workers,
        failure=failure,
        recovery_episode=1 if state == "recovering" else 0,
    )


def test_failover_uses_frozen_snapshot_order_and_same_route_profiles_only():
    snapshot = make_snapshot("pi-pwn-a", "pi-web-a", "pi-web-b", "pi-web-c")
    pools = {pool.profile_id: pool for pool in snapshot.pools}

    chosen = runtime_module.select_runtime_failover(
        snapshot=snapshot,
        failed_pool_id=pools["pi-web-a"].pool_id,
        profile_id="pi-web-a",
        route="web",
    )

    assert chosen == pools["pi-web-b"].pool_id


def test_failover_never_adds_live_configuration_to_active_run():
    snapshot = make_snapshot("pi-web-a", "pi-web-b")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    live_only = make_pool("pi-web-new")

    chosen = runtime_module.select_runtime_failover(
        snapshot=snapshot,
        failed_pool_id=pools["pi-web-a"].pool_id,
        profile_id="pi-web-a",
        route="web",
    )

    assert chosen == pools["pi-web-b"].pool_id
    assert chosen != live_only.pool_id


def test_failover_skips_degraded_stopped_and_recovering_pools():
    snapshot = make_snapshot("pi-web-a", "pi-web-b", "pi-web-c", "pi-web-d")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    views = (
        view(pools["pi-web-a"], "degraded"),
        view(pools["pi-web-b"], "degraded"),
        view(pools["pi-web-c"], "recovering"),
        view(pools["pi-web-d"], "ready"),
    )

    chosen = runtime_module.select_runtime_failover(
        snapshot=snapshot,
        failed_pool_id=pools["pi-web-a"].pool_id,
        profile_id="pi-web-a",
        route="web",
        pool_views=views,
    )

    assert chosen == pools["pi-web-d"].pool_id


@pytest.mark.parametrize("route", ["", "reversing", "unknown-route"])
def test_failover_rejects_unknown_or_invalid_route(route: str):
    snapshot = make_snapshot("pi-web-a", "pi-web-b")
    failed = snapshot.pools[0]

    assert runtime_module.select_runtime_failover(
        snapshot=snapshot,
        failed_pool_id=failed.pool_id,
        profile_id=failed.profile_id,
        route=route,
    ) is None


def test_runtime_unavailable_waits_for_compatible_pool_or_recovery():
    snapshot = make_snapshot("pi-web-a", "pi-web-b")
    a, b = snapshot.pools
    failed = RuntimeFailure("auth", "credential_revoked")

    assert runtime_module.runtime_route_unavailable(
        snapshot=snapshot,
        pool_views=(view(a, "degraded", failure=failed), view(b, "new")),
        profile_id=a.profile_id,
        route="web",
    ) is False
    assert runtime_module.runtime_route_unavailable(
        snapshot=snapshot,
        pool_views=(view(a, "degraded", failure=failed), view(b, "recovering")),
        profile_id=a.profile_id,
        route="web",
    ) is False


def test_runtime_unavailable_waits_for_any_active_worker_even_on_other_route():
    snapshot = make_snapshot("pi-pwn-a", "pi-web-a")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    failed = RuntimeFailure("auth", "credential_revoked")

    unavailable = runtime_module.runtime_route_unavailable(
        snapshot=snapshot,
        pool_views=(
            view(pools["pi-pwn-a"], "ready", active_workers=1),
            view(pools["pi-web-a"], "degraded", failure=failed),
        ),
        profile_id="pi-web-a",
        route="web",
    )

    assert unavailable is False


def test_runtime_unavailable_only_after_frozen_candidates_are_terminal():
    snapshot = make_snapshot("pi-pwn-a", "pi-web-a", "pi-web-b")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    failed = RuntimeFailure("auth", "credential_revoked")

    unavailable = runtime_module.runtime_route_unavailable(
        snapshot=snapshot,
        pool_views=(
            view(pools["pi-pwn-a"], "stopped"),
            view(pools["pi-web-a"], "degraded", failure=failed),
            view(pools["pi-web-b"], "stopped"),
        ),
        profile_id="pi-web-a",
        route="web",
    )

    assert unavailable is True


def test_runtime_failover_diagnostic_has_machine_safe_allowlist_only():
    payload = runtime_module.runtime_failover_diagnostic(
        failed_pool_id="pool-v1::" + "a" * 40,
        chosen_pool_id="pool-v1::" + "b" * 40,
        failure=RuntimeFailure("infrastructure", "runtime_link_lost"),
    )

    assert payload == {
        "failed_pool_id": "pool-v1::" + "a" * 40,
        "chosen_pool_id": "pool-v1::" + "b" * 40,
        "failure_code": "runtime_link_lost",
    }


class _Projection:
    env = {}

    def close(self) -> None:
        return None


class _Projector:
    def project(self, **_kwargs):
        return _Projection()


class _Probe:
    async def run(self, *, pool_spec, **_kwargs):
        return RuntimeProbeResult(
            ready=True,
            probe_id=f"probe-{pool_spec.profile_id}",
            failure=None,
            cache_identity=f"cache-{pool_spec.profile_id}",
        )


class _Executor:
    def __init__(self, pool_id: str, generation: int, *, cleanup_proven: bool = True):
        self.pool_instance_id = f"instance-{pool_id[-8:]}-{generation}"
        self.cleanup_proven = cleanup_proven
        self.terminate_calls = 0

    async def terminate(self, *, require_proof: bool = False):
        self.terminate_calls += 1
        if require_proof and not self.cleanup_proven:
            raise RuntimeFailure("infrastructure", "cleanup_unproven")


class _Factory:
    def __init__(self, *, cleanup_proven: bool = True):
        self.cleanup_proven = cleanup_proven
        self.executors: list[_Executor] = []

    async def __call__(self, *, pool_spec, generation: int, **_kwargs):
        executor = _Executor(pool_spec.pool_id, generation, cleanup_proven=self.cleanup_proven)
        self.executors.append(executor)
        return executor


@pytest.mark.asyncio
async def test_pool_failure_is_local_and_does_not_release_other_pool_worker():
    snapshot = make_snapshot("pi-pwn-a", "pi-web-a")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    manager = ContainerPoolManager(
        run_id=snapshot.run_id,
        snapshot=snapshot,
        executor_factory=_Factory(),
        probe=_Probe(),
        credential_projector=_Projector(),
    )
    pwn = await manager.acquire(
        pool_id=pools["pi-pwn-a"].pool_id,
        worker_instance_id="pwn-worker",
        operation_kind="ordinary",
    )
    web = await manager.acquire(
        pool_id=pools["pi-web-a"].pool_id,
        worker_instance_id="web-worker",
        operation_kind="ordinary",
    )

    await manager.mark_failure(
        pool_id=web.pool_id,
        pool_instance_id=web.pool_instance_id,
        failure=RuntimeFailure("auth", "credential_revoked"),
    )

    views = {item.pool_id: item for item in manager.snapshot_view()}
    assert views[pwn.pool_id].state == "ready"
    assert views[pwn.pool_id].active_workers == 1
    assert pwn._released is False
    await pwn.release()
    await web.release()
    await manager.close()


@pytest.mark.asyncio
async def test_unproven_infrastructure_cleanup_blocks_replacement():
    snapshot = make_snapshot("pi-web-a")
    pool = snapshot.pools[0]
    factory = _Factory(cleanup_proven=False)
    manager = ContainerPoolManager(
        run_id=snapshot.run_id,
        snapshot=snapshot,
        executor_factory=factory,
        probe=_Probe(),
        credential_projector=_Projector(),
    )
    lease = await manager.acquire(
        pool_id=pool.pool_id,
        worker_instance_id="worker-a",
        operation_kind="ordinary",
    )

    await manager.mark_failure(
        pool_id=pool.pool_id,
        pool_instance_id=lease.pool_instance_id,
        failure=RuntimeFailure("infrastructure", "runtime_link_lost"),
    )

    state = manager.snapshot_view()[0]
    assert state.state == "degraded"
    assert state.generation == 1
    assert len(factory.executors) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_same_route_runtime_failover_retries_frozen_profile_without_direction_override():
    snapshot = make_snapshot("pi-web-a", "pi-web-b")
    pools = {pool.profile_id: pool for pool in snapshot.pools}
    emitted: list[tuple[str, dict[str, object]]] = []
    made: list[str] = []

    class _LaneGate:
        @staticmethod
        def lane_for(*, mode: str, worker_class: str) -> str:
            return "ordinary"

    class _Manager:
        async def mark_failure(self, **_kwargs):
            return True

        def snapshot_view(self):
            return (
                view(pools["pi-web-a"], "degraded", failure=RuntimeFailure("infrastructure", "runtime_link_lost")),
                view(pools["pi-web-b"], "new"),
            )

    class _Worker:
        def __init__(self, engine: str):
            self.engine = engine
            self.solver_id = engine
            self.runtime_pool_id = pools[engine].pool_id
            self.runtime_pool_instance_id = f"instance-{engine}"

        async def run(self):
            if self.engine == "pi-web-a":
                raise RuntimeFailure("infrastructure", "runtime_link_lost")
            return SimpleNamespace(solved=False, engine=self.engine)

    class _Swarm:
        challenge = SimpleNamespace(category="web")
        _worker_lane_gate = _LaneGate()
        shared_graph = None
        runtime_snapshot = snapshot
        pool_manager = _Manager()
        _reason_stop_event = asyncio.Event()

        @staticmethod
        def _healthy_matches(engine: str, healthy: list[str]) -> bool:
            return engine in healthy

        @staticmethod
        def _make_cli_worker(engine: str, **_kwargs):
            made.append(engine)
            return _Worker(engine)

        @staticmethod
        def _release_worker_account(_worker):
            return None

        @staticmethod
        def _cancel_solver(_worker):
            return None

        @staticmethod
        async def _emit_bb_bus(kind: str, **fields):
            emitted.append((kind, fields))

    decision = DispatchDecision(
        intent_id="I-web",
        profile="pi-web-a",
        goal="inspect web target",
        direction="web",
        canonical_direction="web",
        direction_source="model",
        direction_resolution="explicit_canonical",
        mode="explore",
    )
    runtime = runtime_module.SwarmWorkerRuntime(
        _Swarm(), healthy=["pi-web-a", "pi-web-b"]
    )

    outcome = await runtime.run(
        decision,
        AgentProfile(id="pi-web-a", worker_profile="pi-web-a", mode="explore"),
    )

    assert outcome.engine == "pi-web-b"
    assert made == ["pi-web-a", "pi-web-b"]
    assert decision.direction == "web"
    assert [kind for kind, _ in emitted] == ["runtime_failover"]
    assert emitted[0][1] == {
        "failed_pool_id": pools["pi-web-a"].pool_id,
        "chosen_pool_id": pools["pi-web-b"].pool_id,
        "failure_code": "runtime_link_lost",
    }
    assert _Swarm._reason_stop_event.is_set() is False

@pytest.mark.asyncio
async def test_runtime_lease_binding_records_acquired_frozen_identity():
    snapshot = make_snapshot("pi-web-a")
    pool = snapshot.pools[0]
    lease = SimpleNamespace(pool_instance_id="instance-frozen", generation=3)

    class _Manager:
        async def acquire(self, **kwargs):
            assert kwargs["pool_id"] == pool.pool_id
            return lease

    request = runtime_module.RuntimeSpawnRequest(
        profile_id=pool.profile_id,
        worker_instance_id="worker-frozen",
        operation_kind="ordinary",
        mode="explore",
    )
    binding = runtime_module.runtime_lease_factory_for_request(
        snapshot=snapshot,
        pool_manager=_Manager(),
        request=request,
    )

    acquired = await binding("worker-frozen", "ordinary")

    assert acquired is lease
    assert binding.pool_id == pool.pool_id
    assert binding.last_pool_instance_id == "instance-frozen"
    assert binding.last_generation == 3


@pytest.mark.asyncio
async def test_terminal_runtime_failure_sets_unavailable_without_local_fallback():
    snapshot = make_snapshot("pi-web-a")
    pool = snapshot.pools[0]
    made: list[str] = []

    class _LaneGate:
        @staticmethod
        def lane_for(*, mode: str, worker_class: str) -> str:
            return "ordinary"

    class _Manager:
        async def mark_failure(self, **_kwargs):
            return True

        def snapshot_view(self):
            return (
                view(
                    pool,
                    "degraded",
                    failure=RuntimeFailure("auth", "credential_revoked"),
                ),
            )

    class _Worker:
        solver_id = "pi-web-a"
        runtime_pool_id = pool.pool_id
        runtime_pool_instance_id = "instance-pi-web-a"

        async def run(self):
            raise RuntimeFailure("auth", "credential_revoked")

    class _Swarm:
        challenge = SimpleNamespace(category="web")
        _worker_lane_gate = _LaneGate()
        shared_graph = None
        runtime_snapshot = snapshot
        pool_manager = _Manager()
        _reason_stop_event = asyncio.Event()

        @staticmethod
        def _healthy_matches(engine: str, healthy: list[str]) -> bool:
            return engine in healthy

        @staticmethod
        def _make_cli_worker(engine: str, **_kwargs):
            made.append(engine)
            return _Worker()

        @staticmethod
        def _release_worker_account(_worker):
            return None

        @staticmethod
        def _cancel_solver(_worker):
            return None

        @staticmethod
        async def _emit_bb_bus(_kind: str, **_fields):
            return None

    runtime = runtime_module.SwarmWorkerRuntime(_Swarm(), healthy=["pi-web-a"])
    decision = DispatchDecision(
        intent_id="I-terminal",
        profile="pi-web-a",
        goal="inspect web target",
        direction="web",
        canonical_direction="web",
        mode="explore",
    )

    with pytest.raises(RuntimeFailure, match="credential_revoked"):
        await runtime.run(
            decision,
            AgentProfile(id="pi-web-a", worker_profile="pi-web-a", mode="explore"),
        )

    assert runtime.runtime_unavailable is True
    assert _Swarm._reason_stop_event.is_set() is True
    assert made == ["pi-web-a"]