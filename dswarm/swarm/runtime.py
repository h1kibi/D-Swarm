"""Worker runtime abstraction used by the Reason-centered scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable
import uuid

from dswarm.solver.container_pool import RuntimeFailure, RuntimePoolView
from dswarm.solver.direction_rules import DEFAULT_DIRECTION_REGISTRY
from dswarm.solver.runtime_policy import RuntimePolicyError, RuntimeSnapshot
from dswarm.solver.worker_profiles import direction_profile_name
from dswarm.swarm.agents import AgentProfile, DispatchDecision


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
_SELECTABLE_POOL_STATES = frozenset({"new", "starting", "probing", "ready"})
_NONTERMINAL_POOL_STATES = _SELECTABLE_POOL_STATES | {"recovering"}


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


@dataclass
class RuntimeLeaseBinding:
    """Callable lease factory that retains only the acquired frozen pool identity."""

    snapshot: Any
    pool_manager: Any
    request: RuntimeSpawnRequest
    pool_id: str
    last_pool_instance_id: str = ""
    last_generation: int = 0
    worker_env_overlay: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )

    def bind_worker_env(self, env: Mapping[str, str]) -> None:
        """Freeze non-secret per-worker control credentials before acquisition."""
        if self.last_pool_instance_id:
            raise RuntimePolicyError("runtime_worker_env_already_acquired")
        normalized: dict[str, str] = {}
        for key, value in dict(env).items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise RuntimePolicyError("invalid_runtime_worker_env")
            normalized[key] = value
        self.worker_env_overlay = MappingProxyType(normalized)

    async def __call__(self, worker_instance_id: str, operation_kind: str):
        if worker_instance_id != self.request.worker_instance_id:
            raise ValueError("runtime_worker_identity_mismatch")
        if operation_kind != self.request.operation_kind:
            raise ValueError("runtime_operation_kind_mismatch")
        lease = await self.pool_manager.acquire(
            pool_id=self.pool_id,
            worker_instance_id=self.request.worker_instance_id,
            operation_kind=self.request.operation_kind,
        )
        if self.worker_env_overlay:
            base_env = dict(getattr(lease, "worker_env", {}) or {})
            conflicts = {
                key for key, value in self.worker_env_overlay.items()
                if key in base_env and base_env[key] != value
            }
            if conflicts:
                await lease.release()
                raise RuntimePolicyError("runtime_worker_env_conflict")
            base_env.update(self.worker_env_overlay)
            lease.worker_env = MappingProxyType(base_env)
        self.last_pool_instance_id = str(getattr(lease, "pool_instance_id", "") or "")
        self.last_generation = int(getattr(lease, "generation", 0) or 0)
        return lease


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
) -> RuntimeLeaseBinding:
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
    return RuntimeLeaseBinding(
        snapshot=snapshot,
        pool_manager=pool_manager,
        request=request,
        pool_id=pool_id,
    )


def _route_profile(route: str) -> str:
    canonical, _resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(route)
    return direction_profile_name(canonical) if canonical else ""


def _profile_matches_route(profile_id: str, route_profile: str) -> bool:
    profile = str(profile_id or "").strip()
    return bool(
        route_profile
        and (profile == route_profile or profile.startswith(route_profile + "-"))
    )


def _pool_views_by_id(
    pool_views: Iterable[RuntimePoolView] | None,
) -> dict[str, RuntimePoolView] | None:
    if pool_views is None:
        return None
    return {str(view.pool_id): view for view in pool_views}


def select_runtime_failover(
    *,
    snapshot: RuntimeSnapshot,
    failed_pool_id: str,
    profile_id: str,
    route: str,
    pool_views: Iterable[RuntimePoolView] | None = None,
) -> str | None:
    """Choose the first eligible same-route pool from the run-frozen snapshot."""

    del profile_id  # The route owns compatibility; the profile is audit context only.
    route_profile = _route_profile(route)
    if not route_profile:
        return None
    views = _pool_views_by_id(pool_views)
    for pool in snapshot.pools:
        pool_id = str(pool.pool_id)
        if pool_id == failed_pool_id:
            continue
        if not _profile_matches_route(str(pool.profile_id), route_profile):
            continue
        if views is not None:
            pool_view = views.get(pool_id)
            if pool_view is None or pool_view.state not in _SELECTABLE_POOL_STATES:
                continue
        return pool_id
    return None


def runtime_route_unavailable(
    *,
    snapshot: RuntimeSnapshot,
    pool_views: Iterable[RuntimePoolView],
    profile_id: str,
    route: str,
) -> bool:
    """Return true only when no active work or frozen compatible recovery remains."""

    del profile_id  # Kept in the public contract as audit context.
    route_profile = _route_profile(route)
    if not route_profile:
        return False
    views = tuple(pool_views)
    if any(int(view.active_workers) > 0 for view in views):
        return False
    by_id = _pool_views_by_id(views) or {}
    compatible = tuple(
        pool
        for pool in snapshot.pools
        if _profile_matches_route(str(pool.profile_id), route_profile)
    )
    for pool in compatible:
        pool_view = by_id.get(str(pool.pool_id))
        # A missing view is an observation gap, not proof of terminal exhaustion.
        if pool_view is None or pool_view.state in _NONTERMINAL_POOL_STATES:
            return False
    return True


def runtime_failover_diagnostic(
    *,
    failed_pool_id: str,
    chosen_pool_id: str,
    failure: RuntimeFailure,
) -> dict[str, str]:
    """Return the complete allowlist for a runtime-only failover event."""

    return {
        "failed_pool_id": str(failed_pool_id),
        "chosen_pool_id": str(chosen_pool_id),
        "failure_code": failure.code,
    }


def _profile_for_pool(snapshot: RuntimeSnapshot, pool_id: str) -> str:
    return next(
        (
            str(pool.profile_id)
            for pool in snapshot.pools
            if str(pool.pool_id) == str(pool_id)
        ),
        "",
    )


def _worker_pool_identity(worker: Any) -> tuple[str, str]:
    binding = getattr(worker, "runtime_lease_binding", None)
    pool_id = str(
        getattr(binding, "pool_id", "")
        or getattr(worker, "runtime_pool_id", "")
        or ""
    )
    pool_instance_id = str(
        getattr(binding, "last_pool_instance_id", "")
        or getattr(worker, "runtime_pool_instance_id", "")
        or ""
    )
    return pool_id, pool_instance_id


class _WorkerRuntimeAttemptFailed(Exception):
    def __init__(self, failure: RuntimeFailure, worker: Any) -> None:
        super().__init__(failure.code)
        self.failure = failure
        self.worker = worker


@runtime_checkable
class WorkerRuntime(Protocol):
    async def run(self, decision: DispatchDecision, profile: AgentProfile) -> Any: ...


class SwarmWorkerRuntime:
    """Adapts Swarm's existing CliSolver construction into WorkerRuntime.

    Runtime failover changes only the frozen profile/pool used to execute an intent.
    The DispatchDecision direction remains untouched, so M4 route diagnostics retain
    their existing meaning.
    """

    def __init__(self, swarm: Any, healthy: list[str], projector: Any = None) -> None:
        self.swarm = swarm
        self.healthy = healthy
        self.projector = projector
        self.runtime_unavailable = False

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

    async def _make_and_run(self, engine: str, make_kwargs: dict[str, Any]) -> Any:
        swarm = self.swarm
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
            # Keep synchronous container startup off the event loop. Shielding lets
            # the cleanup task reclaim a worker that finishes construction after a
            # caller cancellation.
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
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                swarm._cancel_solver(worker)
                raise
            if isinstance(exc, RuntimeFailure):
                # Carry runtime identity outside the frozen machine-safe exception.
                raise _WorkerRuntimeAttemptFailed(exc, worker) from exc
            raise
        finally:
            swarm._release_worker_account(worker)

    async def _handle_runtime_failure(
        self,
        *,
        failure: RuntimeFailure,
        worker: Any,
        decision: DispatchDecision,
        current_engine: str,
        allow_failover: bool,
    ) -> str | None:
        swarm = self.swarm
        snapshot = getattr(swarm, "runtime_snapshot", None)
        manager = getattr(swarm, "pool_manager", None)
        if snapshot is None or manager is None:
            return None

        failed_pool_id, pool_instance_id = _worker_pool_identity(worker)
        if pool_instance_id:
            await manager.mark_failure(
                pool_id=failed_pool_id or None,
                pool_instance_id=pool_instance_id,
                failure=failure,
            )
        pool_views = tuple(manager.snapshot_view())
        if allow_failover and failed_pool_id:
            chosen_pool_id = select_runtime_failover(
                snapshot=snapshot,
                failed_pool_id=failed_pool_id,
                profile_id=current_engine,
                route=decision.canonical_direction or decision.direction,
                pool_views=pool_views,
            )
            if chosen_pool_id:
                chosen_profile = _profile_for_pool(snapshot, chosen_pool_id)
                if chosen_profile:
                    try:
                        await swarm._emit_bb_bus(
                            "runtime_failover",
                            **runtime_failover_diagnostic(
                                failed_pool_id=failed_pool_id,
                                chosen_pool_id=chosen_pool_id,
                                failure=failure,
                            ),
                        )
                    except Exception:
                        # Runtime diagnostics are side effects, never a reason to
                        # fall back to host execution or abandon a frozen candidate.
                        pass
                    return chosen_profile

        if runtime_route_unavailable(
            snapshot=snapshot,
            pool_views=pool_views,
            profile_id=current_engine,
            route=decision.canonical_direction or decision.direction,
        ):
            self.runtime_unavailable = True
            stop_event = getattr(swarm, "_reason_stop_event", None)
            if stop_event is not None:
                stop_event.set()
        return None


    async def _run_poc_verifier(
        self, decision: DispatchDecision, profile: AgentProfile, engine: str
    ) -> Any:
        swarm = self.swarm
        from dswarm.swarm.poc_verification import VerificationFailure
        from dswarm.swarm.poc_verification_runtime import (
            VerificationOutcome,
            run_poc_verification,
        )
        from dswarm.solver.poc_verifier import ContainerPocVerifier

        worker_instance_id = uuid.uuid4().hex
        base = {
            "poc_id": decision.poc_id,
            "reproduction_id": decision.reproduction_id,
            "source_finding_id": decision.source_finding_id,
            "intent_id": decision.intent_id,
            "worker_id": worker_instance_id,
        }
        workspace_root = getattr(swarm, "workspace_root", None)
        runtime_policy = getattr(swarm, "runtime_policy", None)
        snapshot = getattr(swarm, "runtime_snapshot", None)
        manager = getattr(swarm, "pool_manager", None)
        if (
            workspace_root is None
            or runtime_policy is None
            or getattr(runtime_policy, "mode", "") != "docker"
            or snapshot is None
            or manager is None
        ):
            return VerificationOutcome(
                status=VerificationFailure.DOCKER_RUNTIME_UNAVAILABLE.value,
                poc_id=str(base["poc_id"] or ""),
                reproduction_id=decision.reproduction_id,
                verification_id="",
                source_finding_id=decision.source_finding_id,
                intent_id=decision.intent_id,
                worker_id=worker_instance_id,
                verified=False,
                failure_reason=VerificationFailure.DOCKER_RUNTIME_UNAVAILABLE.value,
                diagnostics="docker runtime unavailable",
            )

        operation_kind = runtime_operation_for_spawn(
            mode=decision.mode or profile.mode or "review",
            profile_role="review",
            requested=decision.runtime_operation_kind or "review",
        )
        try:
            request = RuntimeSpawnRequest(
                profile_id=str(engine or profile.resolve_worker_profile(swarm.challenge.category)),
                worker_instance_id=worker_instance_id,
                operation_kind=operation_kind,
                mode=decision.mode or profile.mode or "review",
                intent_id=decision.intent_id,
            )
            lease_factory = runtime_lease_factory_for_request(
                snapshot=snapshot, pool_manager=manager, request=request
            )
        except Exception:
            return VerificationOutcome(
                status=VerificationFailure.DOCKER_RUNTIME_UNAVAILABLE.value,
                poc_id=str(base["poc_id"] or ""),
                reproduction_id=decision.reproduction_id,
                verification_id="",
                source_finding_id=decision.source_finding_id,
                intent_id=decision.intent_id,
                worker_id=worker_instance_id,
                verified=False,
                failure_reason=VerificationFailure.DOCKER_RUNTIME_UNAVAILABLE.value,
                diagnostics="docker runtime unavailable",
            )

        return await run_poc_verification(
            base,
            graph=swarm.shared_graph,
            verifier=ContainerPocVerifier(),
            runtime_lease_factory=lease_factory,
            usage_context=SimpleNamespace(
                workspace_root=workspace_root,
                worker_id=worker_instance_id,
                operation_kind=operation_kind,
                timeout=profile.timeout,
                emit_delta=getattr(swarm, "_emit_bb_bus", None),
            ),
        )

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
        if decision.worker_class == "verifier" and getattr(swarm, "shared_graph", None) is not None:
            return await self._run_poc_verifier(decision, profile, engine)
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
            "reproduction_id": decision.reproduction_id,
            "source_finding_id": decision.source_finding_id,
            "runtime_operation_kind": runtime_operation_for_spawn(
                mode=mode,
                profile_role=role,
                requested=decision.runtime_operation_kind,
            ),
        }

        current_engine = engine
        for attempt in range(2):
            try:
                return await self._make_and_run(current_engine, make_kwargs)
            except _WorkerRuntimeAttemptFailed as attempt_failure:
                failure = attempt_failure.failure
                chosen = await self._handle_runtime_failure(
                    failure=failure,
                    worker=attempt_failure.worker,
                    decision=decision,
                    current_engine=current_engine,
                    allow_failover=(attempt == 0),
                )
                if chosen is None:
                    raise failure
                current_engine = chosen
        raise RuntimeFailure("worker", "runtime_failover_exhausted")
