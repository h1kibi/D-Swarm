from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from apps.tui import __main__ as tui_main
from apps.web.run_manager import RunManager
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimePolicy,
    RuntimePolicyError,
    RuntimeResourceSpec,
    RuntimeSnapshot,
    build_runtime_policy,
)
from dswarm.solver.runtime_snapshot import RuntimeSnapshotStore
from dswarm.swarm.swarm import Swarm


def _snapshot(run_id: str, policy: RuntimePolicy | None = None) -> RuntimeSnapshot:
    policy = policy or build_runtime_policy(env={})
    pool = PoolSpec.with_computed_id(
        profile_id="pi-main",
        runtime_kind="pi",
        resolved_image_id="sha256:" + "a" * 64,
        requested_image_ref="worker:test",
        network=RuntimeNetworkSpec(kind="bridge"),
        resources=RuntimeResourceSpec(
            cpus="1",
            memory="1g",
            pids_limit=128,
            tmpfs_bytes=1024,
        ),
        credential_binding_id="pi-main",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2",),
        protocol_version=2,
        pool_max_concurrent_workers=2,
    )
    return RuntimeSnapshot(
        version=1,
        run_id=run_id,
        created_at=1.0,
        runtime_policy=policy,
        shared_uid=1000,
        shared_gid=1000,
        pools=(pool,),
    )


@dataclass
class _Manager:
    run_id: str
    snapshot: RuntimeSnapshot


def _make_swarm(tmp_path: Path, run_id: str = "runtime-run", **kwargs: Any) -> Swarm:
    return Swarm(
        Challenge(id=run_id, name="runtime", category="web"),
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sandbox"),
        artifacts=ArtifactStore(root=tmp_path / "artifacts"),
        graph_dir=tmp_path / "graph",
        run_id=run_id,
        **kwargs,
    )


def test_swarm_uses_injected_frozen_runtime_context(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-run", policy)
    manager = _Manager("runtime-run", snapshot)

    swarm = _make_swarm(
        tmp_path,
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
    )

    assert swarm.runtime_policy is policy
    assert swarm.runtime_snapshot is snapshot
    assert swarm.pool_manager is manager
    assert swarm.worker_backend == "container"
    assert swarm.pool_id_for_profile("pi-main") == snapshot.pools[0].pool_id


@pytest.mark.parametrize(
    ("snapshot_run", "manager_run", "manager_snapshot", "error"),
    [
        ("other", "runtime-run", "same", "runtime_snapshot_run_mismatch"),
        ("runtime-run", "other", "same", "runtime_manager_run_mismatch"),
        ("runtime-run", "runtime-run", "different", "runtime_manager_snapshot_mismatch"),
    ],
)
def test_swarm_rejects_mismatched_runtime_context(
    tmp_path: Path,
    snapshot_run: str,
    manager_run: str,
    manager_snapshot: str,
    error: str,
) -> None:
    policy = build_runtime_policy(env={})
    snapshot = _snapshot(snapshot_run, policy)
    owned = snapshot if manager_snapshot == "same" else _snapshot(snapshot_run, policy)

    with pytest.raises(RuntimePolicyError, match=error):
        _make_swarm(
            tmp_path,
            runtime_policy=policy,
            runtime_snapshot=snapshot,
            pool_manager=_Manager(manager_run, owned),
        )


def test_swarm_rejects_snapshot_policy_mismatch(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    changed = build_runtime_policy(env={}, max_pools_per_run=31)

    snapshot = _snapshot("runtime-run", policy)
    with pytest.raises(RuntimePolicyError, match="runtime_policy_snapshot_mismatch"):
        _make_swarm(
            tmp_path,
            runtime_policy=changed,
            runtime_snapshot=snapshot,
            pool_manager=_Manager("runtime-run", snapshot),
        )


def test_unknown_runtime_profile_is_structured(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-run", policy)
    swarm = _make_swarm(
        tmp_path,
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=_Manager("runtime-run", snapshot),
    )

    with pytest.raises(RuntimePolicyError, match="runtime_profile_not_in_snapshot"):
        swarm.pool_id_for_profile("missing")


def test_python_local_mode_cannot_be_enabled_by_worker_backend_string(tmp_path: Path) -> None:
    with pytest.raises(RuntimePolicyError, match="local_worker_policy_denied"):
        _make_swarm(tmp_path, worker_backend="local", runtime_policy=None)


def test_approved_local_dev_policy_needs_no_snapshot_or_manager(tmp_path: Path) -> None:
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1"},
    )
    swarm = _make_swarm(tmp_path, worker_backend="local", runtime_policy=policy)
    assert swarm.runtime_policy is policy
    assert swarm.runtime_snapshot is None
    assert swarm.pool_manager is None
    assert swarm.worker_backend == "local"


def test_docker_policy_requires_snapshot_and_manager(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    with pytest.raises(RuntimePolicyError, match="runtime_snapshot_required"):
        _make_swarm(tmp_path, runtime_policy=policy)

    snapshot = _snapshot("runtime-run", policy)
    with pytest.raises(RuntimePolicyError, match="runtime_manager_required"):
        _make_swarm(tmp_path, runtime_policy=policy, runtime_snapshot=snapshot)


def test_strict_worker_boundary_rejects_profile_missing_from_snapshot(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-run", policy)
    profile = {
        "id": "pi-other",
        "name": "pi-other",
        "engine": "pi",
        "roles": ["bootstrap"],
        "runtime": "docker-main",
        "enabled": True,
        "max_running": 1,
    }
    swarm = _make_swarm(
        tmp_path,
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=_Manager("runtime-run", snapshot),
        engines=["pi"],
        worker_profiles=[profile],
    )

    with pytest.raises(RuntimePolicyError, match="runtime_profile_not_in_snapshot"):
        swarm._make_cli_worker("pi", mode="bootstrap")
    assert swarm._spawned_total == 0


class _Builder:
    def __init__(self, snapshot: RuntimeSnapshot, *, fail: bool = False) -> None:
        self.snapshot = snapshot
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> RuntimeSnapshot:
        self.calls.append(kwargs)
        if self.fail:
            raise AssertionError("builder must not be consulted")
        return self.snapshot


class _ManagerFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Manager:
        self.calls.append(kwargs)
        return _Manager(kwargs["run_id"], kwargs["snapshot"])


def _ensure(manager: RunManager, run_id: str, policy: RuntimePolicy, **kwargs: Any):
    return manager.ensure_runtime_context(
        run_id,
        policy=policy,
        worker_profiles=[{"id": "pi-main"}],
        runtime_profiles=[{"id": "docker-main"}],
        run_max_workers=2,
        **kwargs,
    )


def test_run_manager_builds_persists_and_reuses_one_runtime_context(tmp_path: Path) -> None:
    policy = build_runtime_policy(env={})
    snapshot = _snapshot("run-ctx", policy)
    builder = _Builder(snapshot)
    factory = _ManagerFactory()
    manager = RunManager(
        sessions_root=tmp_path,
        runtime_snapshot_builder=builder,
        runtime_pool_manager_factory=factory,
    )

    first = _ensure(manager, "run-ctx", policy)
    second = _ensure(manager, "run-ctx", policy)

    assert first == second
    assert first[0] is policy
    assert first[1] is snapshot
    assert first[2] is manager.runs["run-ctx"].pool_manager
    assert len(builder.calls) == 1
    assert len(factory.calls) == 1
    assert RuntimeSnapshotStore(tmp_path).path_for("run-ctx").is_file()


def test_run_manager_loads_frozen_snapshot_without_changed_builder(tmp_path: Path) -> None:
    frozen_policy = build_runtime_policy(env={})
    frozen = _snapshot("run-frozen", frozen_policy)
    RuntimeSnapshotStore(tmp_path).create(frozen)
    changed_policy = build_runtime_policy(env={}, max_pools_per_run=31)
    builder = _Builder(_snapshot("run-frozen", changed_policy), fail=True)
    factory = _ManagerFactory()

    manager = RunManager(
        sessions_root=tmp_path,
        runtime_snapshot_builder=builder,
        runtime_pool_manager_factory=factory,
    )
    run = manager.create("run-frozen")
    active = _ensure(manager, "run-frozen", changed_policy)

    assert builder.calls == []
    assert run.runtime_snapshot == frozen
    assert active[0] == frozen_policy
    assert active[1] is run.runtime_snapshot
    assert active[2] is run.pool_manager
    assert len(factory.calls) == 1


def test_mock_tui_selection_does_not_construct_runtime_context() -> None:
    args = tui_main._parse([])
    calls: list[str] = []

    def runtime_context_factory(*_args: Any, **_kwargs: Any) -> object:
        calls.append("called")
        raise AssertionError("mock TUI must not construct runtime context")

    bus = object()
    cost = object()
    status, driver = tui_main._driver_for_args(
        bus,
        cost,
        "tui-run",
        args,
        runtime_context_factory=runtime_context_factory,
    )
    try:
        assert status.startswith("mock")
        assert calls == []
    finally:
        driver.close()



class _AcquiringManager:
    def __init__(self, snapshot: RuntimeSnapshot, *, failure: BaseException | None = None) -> None:
        self.run_id = snapshot.run_id
        self.snapshot = snapshot
        self.failure = failure
        self.acquire_calls: list[dict[str, str]] = []
        self.executor = object()
        self.leases: list[Any] = []
        self.lease: Any = None

    async def _release(self) -> None:
        return None

    async def acquire(self, **kwargs: str):
        from dswarm.solver.container_pool import WorkerRuntimeLease

        self.acquire_calls.append(dict(kwargs))
        if self.failure is not None:
            raise self.failure
        self.lease = WorkerRuntimeLease(
            pool_id=self.snapshot.pools[0].pool_id,
            pool_instance_id="pool-instance",
            generation=1,
            worker_instance_id=kwargs["worker_instance_id"],
            executor=self.executor,
            credential_projection=object(),
            worker_env={"LEASE_ONLY": "yes"},
            _release_once=self._release,
        )
        self.leases.append(self.lease)
        return self.lease


def _runtime_worker_profile() -> dict[str, Any]:
    return {
        "id": "pi-main",
        "name": "pi-main",
        "engine": "pi",
        "transport": "pi_cli",
        "roles": ["bootstrap", "review", "recon", "explore"],
        "runtime": "docker-main",
        "enabled": True,
        "max_running": 2,
    }


@pytest.mark.asyncio
async def test_strict_docker_worker_injects_frozen_pool_lease_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-run", policy)
    manager = _AcquiringManager(snapshot)
    seen: list[dict[str, Any]] = []

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            self.solver_id = kwargs["solver_label"]
            self.runtime_lease_factory = kwargs.get("runtime_lease_factory")
            self.runtime_policy = kwargs.get("runtime_policy")
            self.container = kwargs.get("container")
            self.worker_env = kwargs.get("worker_env")

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    assert not hasattr(Swarm, "_container_for_engine")
    swarm = _make_swarm(
        tmp_path,
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    worker = swarm._make_cli_worker(
        "pi-main",
        mode="bootstrap",
        task_kind="web",
        runtime_operation_kind="ordinary",
    )

    assert len(seen) == 1
    assert worker.runtime_policy is policy
    assert worker.container is None
    # Strict lease path: worker_env carries NO host credential material, but
    # MUST carry the isolated-HOME/graph context (provider config discovery).
    if worker.worker_env:
        joined = str(worker.worker_env)
        assert "OPENAI_API_KEY" not in joined
        assert "DEEPSEEK_API_KEY" not in joined
        # HOME/PI_CODING_AGENT_DIR appear when worker_root is configured
        # (production dispatch always does; this fixture does not).
    assert callable(worker.runtime_lease_factory)
    assert seen[0]["task_kind"] == "web"
    assert seen[0]["runtime_operation_kind"] == "ordinary"
    lease = await worker.runtime_lease_factory(worker.worker_instance_id, "ordinary")
    assert lease is manager.lease
    assert manager.acquire_calls == [{
        "pool_id": snapshot.pools[0].pool_id,
        "worker_instance_id": worker.worker_instance_id,
        "operation_kind": "ordinary",
    }]


def test_local_dev_worker_uses_host_without_runtime_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module

    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1"},
    )
    seen: list[dict[str, Any]] = []

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            self.solver_id = kwargs["solver_label"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    swarm = _make_swarm(
        tmp_path,
        worker_backend="local",
        runtime_policy=policy,
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    worker = swarm._make_cli_worker("pi-main", mode="bootstrap")

    assert worker.gateway_token is None
    assert seen[0].get("runtime_lease_factory") is None
    assert seen[0].get("runtime_policy") is policy
    assert seen[0].get("container") is None


@pytest.mark.asyncio
async def test_strict_docker_gateway_worker_gets_task_token_only_through_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module
    from dswarm.solver.modelgateway import ModelGateway

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-token-run", policy)
    manager = _AcquiringManager(snapshot)
    gateway = ModelGateway(host="127.0.0.1", port=0)

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.solver_id = kwargs["solver_label"]
            self.runtime_lease_factory = kwargs["runtime_lease_factory"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    swarm = _make_swarm(
        tmp_path,
        run_id="runtime-token-run",
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    worker = swarm._make_cli_worker("pi-main", mode="review")

    token = worker.gateway_token
    assert token
    claims = gateway.claims_for_token(token)
    assert claims is not None
    assert claims.run_id == swarm.run_id
    assert claims.worker_instance_id == worker.worker_instance_id
    assert claims.profile_id == "pi-main"
    assert claims.token_scope == "review"

    lease = await worker.runtime_lease_factory(worker.worker_instance_id, "review")
    assert lease.worker_env["DSWARM_TASK_TOKEN"] == token
    assert lease.worker_env["DEEPSEEK_API_KEY"] == token
    assert lease.worker_env["DSWARM_PI_PROVIDER"] == "ctf-gateway"
    assert "OPENAI_API_KEY" not in lease.worker_env


@pytest.mark.asyncio
async def test_strict_docker_workers_receive_independent_claimed_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module
    from dswarm.solver.modelgateway import ModelGateway

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-multi-token-run", policy)
    manager = _AcquiringManager(snapshot)
    gateway = ModelGateway(host="127.0.0.1", port=0)

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.solver_id = kwargs["solver_label"]
            self.runtime_lease_factory = kwargs["runtime_lease_factory"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    swarm = _make_swarm(
        tmp_path,
        run_id="runtime-multi-token-run",
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    workers = [
        swarm._make_cli_worker("pi-main", mode="bootstrap"),
        swarm._make_cli_worker("pi-main", mode="review"),
        swarm._make_cli_worker("pi-main", mode="bootstrap", profile_role="recon"),
    ]
    operations = ["bootstrap", "review", "recon"]
    tokens = [worker.gateway_token for worker in workers]

    assert all(tokens)
    assert len(set(tokens)) == 3
    claims = [gateway.claims_for_token(token) for token in tokens]
    assert [claim.token_scope for claim in claims] == ["worker", "review", "recon"]
    assert all(claim.run_id == swarm.run_id for claim in claims)
    assert len({claim.worker_instance_id for claim in claims}) == 3

    for worker, operation, token in zip(workers, operations, tokens):
        lease = await worker.runtime_lease_factory(worker.worker_instance_id, operation)
        assert lease.worker_env["DSWARM_TASK_TOKEN"] == token
        assert lease.worker_env["DEEPSEEK_API_KEY"] == token

    swarm._release_worker_account(workers[0])
    assert gateway.claims_for_token(tokens[0]) is None
    assert gateway.claims_for_token(tokens[1]) is not None
    assert gateway.claims_for_token(tokens[2]) is not None


def test_strict_docker_constructor_failure_rolls_back_gateway_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module
    from dswarm.solver.modelgateway import ModelGateway

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-constructor-failure", policy)
    gateway = ModelGateway(host="127.0.0.1", port=0)

    class ExplodingCliSolver:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("fake worker construction failed")

    monkeypatch.setattr(cli_solver_module, "CliSolver", ExplodingCliSolver)
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    swarm = _make_swarm(
        tmp_path,
        run_id="runtime-constructor-failure",
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=_AcquiringManager(snapshot),
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    with pytest.raises(RuntimeError, match="fake worker construction failed"):
        swarm._make_cli_worker("pi-main", mode="bootstrap")

    assert gateway._tokens == {}
    assert gateway._run_tokens == {}


def test_strict_docker_account_claim_failure_rolls_back_gateway_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module
    from dswarm.solver.modelgateway import ModelGateway

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-claim-failure", policy)
    gateway = ModelGateway(host="127.0.0.1", port=0)

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.solver_id = kwargs["solver_label"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    swarm = _make_swarm(
        tmp_path,
        run_id="runtime-claim-failure",
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=_AcquiringManager(snapshot),
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )
    monkeypatch.setattr(
        swarm, "_claim_worker_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("account claim failed")),
    )

    with pytest.raises(RuntimeError, match="account claim failed"):
        swarm._make_cli_worker("pi-main", mode="bootstrap")

    assert gateway._tokens == {}
    assert gateway._run_tokens == {}


@pytest.mark.asyncio
async def test_strict_docker_explicit_endpoint_does_not_issue_gateway_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module
    from dswarm.solver.modelgateway import ModelGateway

    policy = build_runtime_policy(env={})
    snapshot = _snapshot("runtime-direct-run", policy)
    manager = _AcquiringManager(snapshot)
    gateway = ModelGateway(host="127.0.0.1", port=0)
    profile = {
        **_runtime_worker_profile(),
        "base_url": "https://llm.example/v1",
        "api_key_ref": "CUSTOM_API_KEY",
    }

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.solver_id = kwargs["solver_label"]
            self.runtime_lease_factory = kwargs["runtime_lease_factory"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    swarm = _make_swarm(
        tmp_path,
        run_id="runtime-direct-run",
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
        engines=["pi-main"],
        worker_profiles=[profile],
    )

    worker = swarm._make_cli_worker("pi-main", mode="bootstrap")
    lease = await worker.runtime_lease_factory(worker.worker_instance_id, "bootstrap")

    assert worker.gateway_token is None
    assert "DSWARM_TASK_TOKEN" not in lease.worker_env
    assert "DEEPSEEK_API_KEY" not in lease.worker_env
    assert gateway._tokens == {}


def test_worker_factory_preserves_verifier_linkage_without_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver import cli_solver as cli_solver_module

    seen: list[dict[str, Any]] = []

    class FakeCliSolver:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            self.solver_id = kwargs["solver_label"]

    monkeypatch.setattr(cli_solver_module, "CliSolver", FakeCliSolver)
    swarm = _make_swarm(
        tmp_path,
        engines=["pi-main"],
        worker_profiles=[_runtime_worker_profile()],
    )

    worker = swarm._make_cli_worker(
        "pi-main",
        mode="review",
        intent_goal="Run the saved PoC entrypoint",
        intent_id="intent-verifier",
        reproduction_id="poc-repro::artifact::digest",
        source_finding_id="finding-1",
    )

    assert worker.solver_id
    assert seen[0]["reproduction_id"] == "poc-repro::artifact::digest"
    assert seen[0]["source_finding_id"] == "finding-1"
    assert "command" not in seen[0]
    assert "entry_command" not in seen[0]
