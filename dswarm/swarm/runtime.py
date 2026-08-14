"""Worker runtime abstraction used by the Reason-centered scheduler."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from dswarm.swarm.agents import AgentProfile, DispatchDecision


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
