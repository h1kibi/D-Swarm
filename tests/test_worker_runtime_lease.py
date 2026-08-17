from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver.cli_solver import CliSolver
from dswarm.solver.container_pool import RuntimeFailure, WorkerRuntimeLease
from dswarm.solver.runtime_policy import RuntimePolicyError, build_runtime_policy
from dswarm.solver.types import SolveOutcome


class _Executor:
    pass


class _LeaseFactory:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.release_calls = 0
        self.failure = failure
        self.executor = _Executor()
        self.acquire_started = asyncio.Event()
        self.acquire_gate: asyncio.Event | None = None
        self.release_observer = None
        self.lease = WorkerRuntimeLease(
            pool_id="pool-v1::" + "a" * 40,
            pool_instance_id="pool-instance",
            generation=1,
            worker_instance_id="placeholder",
            executor=self.executor,
            credential_projection=object(),
            worker_env={"LEASE_ONLY": "yes"},
            _release_once=self._release_once,
        )

    async def _release_once(self) -> None:
        if self.release_observer is not None:
            self.release_observer()
        self.release_calls += 1

    async def __call__(
        self, worker_instance_id: str, operation_kind: str
    ) -> WorkerRuntimeLease:
        self.calls.append((worker_instance_id, operation_kind))
        self.acquire_started.set()
        if self.acquire_gate is not None:
            await self.acquire_gate.wait()
        if self.failure is not None:
            raise self.failure
        self.lease.worker_instance_id = worker_instance_id
        return self.lease


def _solver(
    factory: _LeaseFactory | None,
    *,
    task_kind: str = "",
    runtime_policy=None,
) -> CliSolver:
    challenge = Challenge(id="lease-run", name="lease", category="web")
    solver = CliSolver(
        None,
        challenge,
        engine="pi",
        solver_label="cli-pi-lease",
        worker_env={"LEGACY_ENV": "must-not-survive-docker-acquire"},
        runtime_lease_factory=factory,
        runtime_policy=runtime_policy or build_runtime_policy(env={}),
        task_kind=task_kind,
    )
    solver.worker_instance_id = "worker-instance-1"
    return solver


def _outcome(solver: CliSolver) -> SolveOutcome:
    return SolveOutcome(False, None, 1, solver.graph, "done")


def _install_body(solver: CliSolver, body) -> None:
    solver._run_bootstrap = MethodType(body, solver)


@pytest.mark.asyncio
async def test_cli_solver_acquires_before_online_and_releases_once_on_success() -> None:
    order: list[str] = []
    factory = _LeaseFactory()
    original_call = factory.__call__

    async def acquire(worker_instance_id: str, operation_kind: str):
        order.append("acquire")
        return await original_call(worker_instance_id, operation_kind)

    solver = _solver(factory, task_kind="ordinary")
    solver.runtime_lease_factory = acquire

    async def emit_status(self, *, online: bool, reason: str, status=None) -> None:
        del self, reason, status
        order.append("online" if online else "offline")

    async def emit_lifecycle(self, kind: str, **fields: Any) -> None:
        del self, fields
        order.append(kind)

    async def body(self) -> SolveOutcome:
        order.append("driver")
        assert self.container is factory.executor
        env = self._worker_env()
        assert env["LEASE_ONLY"] == "yes"
        assert "LEGACY_ENV" not in env
        return _outcome(self)

    solver._emit_worker_status = MethodType(emit_status, solver)
    solver._emit_lifecycle = MethodType(emit_lifecycle, solver)
    _install_body(solver, body)

    result = await solver.run()

    assert result.reason == "done"
    assert factory.calls == [("worker-instance-1", "ordinary")]
    assert factory.release_calls == 1
    assert solver.container is factory.executor
    assert order[:4] == ["acquire", "online", "spawned", "driver"]
    assert order[-1] == "offline"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "raised"])
async def test_cli_solver_releases_lease_on_every_terminal(terminal: str) -> None:
    factory = _LeaseFactory()
    solver = _solver(factory)
    body_started = asyncio.Event()

    async def body(self) -> SolveOutcome:
        body_started.set()
        if terminal == "raised":
            raise RuntimeError("worker failed")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    _install_body(solver, body)

    if terminal == "cancelled":
        task = asyncio.create_task(solver.run())
        await body_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="worker failed"):
            await solver.run()

    assert factory.release_calls == 1


@pytest.mark.asyncio
async def test_cli_solver_reuses_one_lease_for_all_worker_turns() -> None:
    factory = _LeaseFactory()
    solver = _solver(factory)

    async def body(self) -> SolveOutcome:
        for _ in range(3):
            assert self.container is factory.executor
            assert self._worker_env()["LEASE_ONLY"] == "yes"
        return _outcome(self)

    _install_body(solver, body)

    await solver.run()

    assert factory.calls == [("worker-instance-1", "bootstrap")]
    assert factory.release_calls == 1


@pytest.mark.asyncio
async def test_cli_solver_acquire_failure_never_announces_online_or_runs_worker() -> None:
    failure = RuntimeFailure(category="infrastructure", code="pool_unavailable")
    factory = _LeaseFactory(failure=failure)
    solver = _solver(factory)
    statuses: list[tuple[bool, str]] = []
    body_called = False

    async def emit_status(self, *, online: bool, reason: str, status=None) -> None:
        del self, status
        statuses.append((online, reason))

    async def body(self) -> SolveOutcome:
        nonlocal body_called
        body_called = True
        return _outcome(self)

    solver._emit_worker_status = MethodType(emit_status, solver)
    _install_body(solver, body)

    with pytest.raises(RuntimeFailure) as raised:
        await solver.run()

    assert raised.value is failure
    assert body_called is False
    assert all(online is False for online, _reason in statuses)
    assert factory.release_calls == 0


@pytest.mark.asyncio
async def test_cli_solver_cancelled_while_waiting_releases_no_nonexistent_lease() -> None:
    factory = _LeaseFactory()
    factory.acquire_gate = asyncio.Event()
    solver = _solver(factory)

    async def body(self) -> SolveOutcome:
        raise AssertionError("worker must not start before capacity is acquired")

    _install_body(solver, body)
    task = asyncio.create_task(solver.run())
    await factory.acquire_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory.release_calls == 0
    assert solver.container is None


@pytest.mark.asyncio
async def test_cli_solver_revokes_gateway_token_before_releasing_lease() -> None:
    factory = _LeaseFactory()
    solver = _solver(factory)
    solver.gateway_token = "already-revoked-is-idempotent"
    factory.release_observer = lambda: (
        None if solver.gateway_token is None else (_ for _ in ()).throw(
            AssertionError("gateway token must be revoked before lease release")
        )
    )

    async def body(self) -> SolveOutcome:
        return _outcome(self)

    _install_body(solver, body)

    await solver.run()

    assert solver.gateway_token is None
    assert factory.release_calls == 1


@pytest.mark.asyncio
async def test_gateway_revoke_failure_keeps_token_for_run_level_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dswarm.solver.modelgateway import ModelGateway

    factory = _LeaseFactory()
    solver = _solver(factory)
    solver.gateway_token = "retry-token"

    class FailingGateway:
        def revoke_token(self, token: str) -> None:
            assert token == "retry-token"
            raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: FailingGateway()))

    async def body(self) -> SolveOutcome:
        return _outcome(self)

    _install_body(solver, body)

    await solver.run()

    assert solver.gateway_token == "retry-token"
    assert factory.release_calls == 1


@pytest.mark.asyncio
async def test_local_dev_policy_uses_host_without_constructing_a_lease() -> None:
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1"},
    )
    challenge = Challenge(id="local-run", name="local", category="web")
    solver = CliSolver(
        None,
        challenge,
        engine="pi",
        runtime_policy=policy,
        runtime_lease_factory=None,
    )

    async def body(self) -> SolveOutcome:
        assert self.container is None
        return _outcome(self)

    _install_body(solver, body)

    await solver.run()

    assert solver.container is None



def test_docker_policy_rejects_missing_runtime_lease_factory() -> None:
    challenge = Challenge(id="strict-run", name="strict", category="web")

    with pytest.raises(RuntimePolicyError, match="runtime_lease_factory_required"):
        CliSolver(
            None,
            challenge,
            engine="pi",
            runtime_policy=build_runtime_policy(env={}),
            runtime_lease_factory=None,
        )


def test_local_dev_policy_rejects_runtime_lease_factory() -> None:
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1"},
    )
    challenge = Challenge(id="local-run", name="local", category="web")

    with pytest.raises(RuntimePolicyError, match="local_runtime_lease_forbidden"):
        CliSolver(
            None,
            challenge,
            engine="pi",
            runtime_policy=policy,
            runtime_lease_factory=_LeaseFactory(),
        )
