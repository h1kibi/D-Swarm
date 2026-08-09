"""Worker runtime abstraction used by the Reason-centered scheduler."""

from __future__ import annotations

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
        role = (
            "recon" if mode == "recon"
            else "review" if mode == "review"
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
        worker = swarm._make_cli_worker(
            engine,
            mode=mode,
            intent_goal=decision.goal,
            intent_id=decision.intent_id,
            profile_role=role,
            timeout_override=profile.timeout,
            task_kind=decision.task_kind or swarm.challenge.category,
            host_scan=decision.host_scan,
        )
        try:
            outcome = await worker.run()
            if swarm.shared_graph is not None and self.projector is not None:
                self.projector.sync(swarm.shared_graph)
            return outcome
        finally:
            swarm._release_worker_account(worker)
