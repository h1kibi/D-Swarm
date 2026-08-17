"""Worker runtime abstraction used by the Reason-centered scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from dswarm.swarm.agents import AgentProfile, DispatchDecision
from dswarm.solver.runtime_policy import RuntimePolicyError


RuntimeOperationKind = Literal[
    "bootstrap",
    "ordinary",
    "review",
    "recon",
    "recovery",
    "standby",
    "resolve",
    "btw",
]
_RUNTIME_OPERATION_KINDS = frozenset(
    {"bootstrap", "ordinary", "review", "recon", "recovery", "standby", "resolve", "btw"}
)


@dataclass(frozen=True)
class RuntimeSpawnRequest:
    """Frozen audit identity for one real worker runtime acquisition."""

    profile_id: str
    worker_instance_id: str
    operation_kind: RuntimeOperationKind
    mode: str
    intent_id: str = ""

    def __post_init__(self) -> None:
        if self.operation_kind not in _RUNTIME_OPERATION_KINDS:
            raise ValueError("invalid_runtime_operation_kind")


def runtime_operation_for_spawn(
    *, mode: str, profile_role: str = "", requested: str = ""
) -> RuntimeOperationKind:
    """Resolve the audited runtime operation independently of business task kind."""

    if requested:
        if requested not in _RUNTIME_OPERATION_KINDS:
            raise ValueError("invalid_runtime_operation_kind")
        return requested  # type: ignore[return-value]
    if profile_role == "recon" or mode == "recon":
        return "recon"
    if mode == "review":
        return "review"
    if mode == "explore":
        return "ordinary"
    return "bootstrap"


def runtime_lease_factory_for_request(
    *,
    snapshot: Any,
    pool_manager: Any,
    request: RuntimeSpawnRequest,
):
    """Bind one worker request to its frozen profile-to-pool mapping."""

    pool_id = next(
        (
            str(pool.pool_id)
            for pool in snapshot.pools
            if str(pool.profile_id) == request.profile_id
        ),
        "",
    )
    if not pool_id:
        raise RuntimePolicyError("runtime_profile_not_in_snapshot")

    async def acquire(worker_instance_id: str, operation_kind: str):
        if worker_instance_id != request.worker_instance_id:
            raise ValueError("runtime_worker_identity_mismatch")
        if operation_kind != request.operation_kind:
            raise ValueError("runtime_operation_kind_mismatch")
        return await pool_manager.acquire(
            pool_id=pool_id,
            worker_instance_id=request.worker_instance_id,
            operation_kind=request.operation_kind,
        )

    return acquire


@runtime_checkable
class WorkerRuntime(Protocol):
    async def run(self, decision: DispatchDecision, profile: AgentProfile) -> Any: ...


class SwarmWorkerRuntime:
    """Adapts Swarm's existing CliSolver construction into WorkerRuntime.

    Reason may ask for a cross-direction profile on a composite challenge. If that
    profile is unavailable, the intent is not dropped: it falls back to the current
    challenge's primary direction worker and a warning is surfaced on the board.
    """

    def __init__(self, swarm: Any, healthy: list[str], projector: Any = None) -> None:
        self.swarm = swarm
        self.healthy = healthy
        self.projector = projector

    async def _warn(self, decision: DispatchDecision, message: str) -> None:
        try:
            await self.swarm._emit_bb_bus(
                "worker_spawn_rejected",
                intent_id=decision.intent_id,
                profile=decision.profile,
                reason=message,
            )
        except Exception:
            pass

    async def run(self, decision: DispatchDecision, profile: AgentProfile) -> Any:
        swarm = self.swarm
        mode = decision.mode or profile.mode or "explore"
        lane = swarm._worker_lane_gate.lane_for(
            mode=mode, worker_class=decision.worker_class
        )
        role = (
            "recon" if mode == "recon"
            else "review" if lane == "review"
            else "explore"
        )
        engine = profile.resolve_worker_profile(swarm.challenge.category)
        primary = profile.resolve_worker_profile(swarm.challenge.category)
        if not swarm._healthy_matches(engine, self.healthy):
            try:
                engine = swarm._pick_engine([], self.healthy, role=role)
            except RuntimeError:
                engine = primary
            if engine != primary:
                await self._warn(
                    decision,
                    f"profile {decision.profile} unavailable; falling back to {engine}",
                )
        make_kwargs = {
            "mode": mode,
            "intent_goal": decision.goal,
            "intent_id": decision.intent_id,
            "profile_role": role,
            "timeout_override": profile.timeout,
            "task_kind": decision.task_kind or swarm.challenge.category,
            "host_scan": decision.host_scan,
            "runtime_operation_kind": runtime_operation_for_spawn(
                mode=mode,
                profile_role=role,
                requested=decision.runtime_operation_kind,
            ),
        }
        loop = asyncio.get_running_loop()
        create_future = loop.run_in_executor(
            None, lambda: swarm._make_cli_worker(engine, **make_kwargs)
        )
        worker = None

        async def _cancel_late_created_worker() -> None:
            try:
                late_worker = await create_future
            except BaseException:
                return
            try:
                swarm._cancel_solver(late_worker)
            finally:
                swarm._release_worker_account(late_worker)

        try:
            # Worker construction can synchronously start/wait for a Docker
            # container. Keep that off the event loop so startup-test / stop /
            # delete timeouts can still fire. Shield the executor future so task
            # cancellation does not discard a late-created worker; the cleanup
            # task below cancels and releases it once construction returns.
            worker = await asyncio.shield(create_future)
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                asyncio.create_task(_cancel_late_created_worker())
            raise

        try:
            outcome = await worker.run()
            if swarm.shared_graph is not None and self.projector is not None:
                self.projector.sync(swarm.shared_graph)
            return outcome
        except BaseException as exc:
            # asyncio task cancellation alone does not stop the shelled CLI
            # worker's subprocess / to_thread runner. Signal the underlying
            # solver before unwinding so RunManager.delete() and ReasonSwarm
            # cancellation cannot leave a live worker that later recreates the
            # run container.
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                swarm._cancel_solver(worker)
            raise
        finally:
            swarm._release_worker_account(worker)
