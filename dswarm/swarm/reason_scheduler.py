"""Reason-centered swarm scheduler.

A run starts with one recon worker,
then Reason audits the board and produces DispatchDecisions. The scheduler
only executes those decisions; it never plans an attack path itself.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.core.provider_errors import (
    ProviderErrorAggregator,
    ProviderErrorDiagnostic,
    classify_provider_error,
)
from dswarm.models.solve_graph import Challenge
from dswarm.swarm import energy, energy_capture
from dswarm.swarm.agents import AgentProfile, AgentRegistry, DispatchDecision
from dswarm.swarm.board import (
    Board,
    FindingKind,
    FindingPredicate,
    MemoryBoard,
    PheromoneSettings,
)
from dswarm.solver.direction_rules import (
    DEFAULT_DIRECTION_REGISTRY,
    sanitize_raw_direction,
)
from dswarm.swarm.poc_verification import sanitize_public_text
from dswarm.solver.worker_profiles import direction_profile_name
from dswarm.swarm.lane_gate import WorkerLaneGate
from dswarm.solver.reason import ReasonResult
from dswarm.swarm.priority import normalize_priority

WorkerFactory = Callable[
    [DispatchDecision, AgentProfile],
    Awaitable[Any],
]

# docs/17 §5.4: these provider failures open the current override's circuit on the
# FIRST occurrence instead of waiting for the bounded cycle budget. The host
# still probes the settings default before switching (trigger_role_fallback).
# Two spellings are accepted because the planner diagnostic can come from
# ``classify_llm_exception`` (http_401/http_403/missing_api_key/model_not_found/
# invalid_base_url) or from a provider-classified launch diagnostic
# (insufficient_quota/auth_invalid/model_not_found).
_IMMEDIATE_CIRCUIT_CODES = frozenset({
    "missing_api_key", "http_401", "http_403",
    "insufficient_quota", "auth_invalid",
    "model_not_found", "invalid_base_url",
})

# ``_planner_exception_diag`` must not overwrite a specific, already-classified
# provider failure with a generic fallback classification.
_GENERIC_PLANNER_CODES = frozenset({"network_error", "planner_exception"})

# docs/17 §5.4: one Reason cycle makes at most this many requests (with the
# existing backoff) before the cycle is counted as unavailable.
_REASON_REQUESTS_PER_CYCLE = 3

# The host never fabricates a "majority of active workers" ratio for the single
# instance Reason/Reviewer roles: only the Worker role aggregates provider
# failures across workers, so those roles pass affected_workers=0 and switch on
# the documented consecutive-failure / fatal path instead (docs/17 §5.3).
_REASON_RETRY_WINDOW_S = 60


def _dedupe_key(decision: DispatchDecision) -> str:
    goal = re.sub(r"[^a-z0-9]+", " ", decision.goal.lower()).strip()
    return f"{decision.profile}:{decision.mode}:{goal}"


class ReasonSwarm:
    """Thin scheduler that executes Reason dispatch decisions."""

    def __init__(
        self,
        challenge: Challenge,
        *,
        board: Optional[Board] = None,
        agents: Optional[AgentRegistry] = None,
        llm: Any = None,
        reason_model: str = "deepseek-v4-pro",
        bus: Any = None,
        run_id: Optional[str] = None,
        max_workers: int = 2,
        wall_clock_budget: float = float("inf"),
        poll_interval: float = 0.5,
        worker_factory: Optional[WorkerFactory] = None,
        reason_fn: Optional[Callable[[str, str], Awaitable[ReasonResult]]] = None,
        pheromone: Optional[PheromoneSettings] = None,
        stop_event: Optional[asyncio.Event] = None,
        graph: Any = None,
        projector: Any = None,
        max_intents_per_reason: int = 4,
        reason_debounce: float = 1.0,
        pause_event: Optional[asyncio.Event] = None,
        planner_diagnostic: Optional[dict[str, Any]] = None,
        review_max_concurrent: int = 1,
        lane_gate: Optional[WorkerLaneGate] = None,
        energy_trace_enabled: bool = False,
        energy_trace_sink: Any = None,
        initial_runtime_operation_kind: str = "",
        intent_review_fn: Optional[Callable[[DispatchDecision, str], Awaitable[dict[str, Any]]]] = None,
        # Production enables this explicitly; direct/unit scheduler fixtures keep
        # the historical permissive behavior when no Reviewer is wired.
        require_reviewer: bool = False,
        evidence_review_fn: Optional[Callable[[], Awaitable[Any]]] = None,
        owner_swarm: Any = None,
    ) -> None:
        self.challenge = challenge
        self.board = board or MemoryBoard(challenge.id, pheromone=pheromone)
        self.agents = agents or AgentRegistry()
        self.llm = llm
        self.reason_model = reason_model
        self.bus = bus
        self.run_id = run_id
        self.max_workers = max(1, int(max_workers))
        self.wall_clock_budget = wall_clock_budget
        self.poll_interval = poll_interval
        self.worker_factory = worker_factory
        self.reason_fn = reason_fn
        self.intent_review_fn = intent_review_fn
        self.stop_event = stop_event
        self.graph = graph
        self.projector = projector
        self.owner_swarm = owner_swarm
        self.evidence_review_fn = evidence_review_fn
        self.max_intents_per_reason = max(1, int(max_intents_per_reason))
        self.reason_debounce = float(reason_debounce)
        self.pause_event = pause_event
        self.lane_gate = lane_gate or WorkerLaneGate(
            max_workers=self.max_workers,
            review_max_concurrent=max(0, int(review_max_concurrent)),
        )
        # M7 offline energy telemetry (docs/10 Contract v9.2). Off by default;
        # zero side effects unless explicitly enabled. Enabled without a sink
        # is a construction error ("no sink, no capture").
        self.energy_trace_enabled = bool(energy_trace_enabled)
        self.energy_trace_sink = energy_trace_sink
        self.initial_runtime_operation_kind = str(initial_runtime_operation_kind or "")
        # Reviewer decisions are an independent dispatch gate. Production must
        # fail closed when its callback is absent, while legacy fixtures retain
        # compatibility unless they opt into the explicit gate.
        self.require_reviewer = bool(require_reviewer)
        self._reviewer_available = intent_review_fn is not None
        self._reviewer_failure_noted = False
        self._reviewer_blocked = False
        if self.energy_trace_enabled and self.energy_trace_sink is None:
            raise ValueError(
                "energy_trace_enabled=True requires an energy_trace_sink "
                "(EnergyTraceSink); construct the sink before the swarm")
        # Cross-resume-unique trace identity component.
        self._instance_uuid = str(uuid.uuid4())
        self._planner_failures = 0
        self._reason_health_state = "healthy"
        self._reason_cycle_failures = 0
        self._reason_health_payload: dict[str, Any] = {}
        self._reason_circuit_open = False
        self._last_planner_error: dict[str, Any] = dict(planner_diagnostic or {})
        self._max_planner_failures = int(
            os.environ.get("DSWARM_REASON_MAX_PLANNER_FAILURES", "6") or 6)
        self._executed: set[str] = set()
        self._fallback_executed = False
        self._generation = 0
        # First flag-bearing worker outcome.  The outer Swarm needs the real CLI
        # session/workdir to persist winner.json; reducing Reason results to strings
        # used to discard that continuation handle.
        self._winner_outcome: Any = None
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_pending: set[str] = set()
        self._max_recovery_attempts = int(
            os.environ.get("DSWARM_WORKER_PROVIDER_RECOVERY_ATTEMPTS", "2") or 2)
        self._paused_profiles: set[str] = set()
        self._direction_overrides: list[dict[str, Any]] = []
        self._provider_errors = ProviderErrorAggregator(
            window_s=float(os.environ.get("DSWARM_PROVIDER_ERROR_WINDOW_S", "60") or 60),
            fatal_threshold=int(os.environ.get("DSWARM_PROVIDER_FATAL_THRESHOLD", "3") or 3),
            majority_ratio=float(os.environ.get("DSWARM_PROVIDER_MAJORITY_RATIO", "0.5") or 0.5),
        )
        # One bounded diagnostic per intent registration failure.  Intent
        # persistence is best-effort so a graph outage cannot stop dispatch,
        # but a lost row must remain visible to operators.
        self._intent_registration_failures_noted: set[str] = set()

    async def _emit(self, delta_type: str, *, stage: Optional[str] = None, **fields: Any) -> None:
        """Structured observability delta (D-Swarm Phase 2, docs/07 §7.1).

        Everything goes out as a BLACKBOARD_DELTA distinguished by the
        payload's ``delta_type``/``kind`` — no new SSE event types. Silent
        when no bus is wired; a bus failure never affects scheduling.
        """
        if self.bus is None:
            return
        try:
            if stage is not None:
                fields["stage"] = stage
            await self.bus.emit(Event(
                event_type=EventType.BLACKBOARD_DELTA,
                run_id=self.run_id or self.challenge.id,
                challenge_id=self.challenge.id,
                payload=blackboard_delta_payload(
                    delta_type, actor="reason", delta_type=delta_type, **fields),
            ))
        except Exception:
            pass

    def _open_operator_decision(self) -> Optional[DispatchDecision]:
        """The highest-priority open operator-directive intent, as a decision.

        Operator hints/focus/redirect intents (``I-<directive_id>``) are proposed
        by the coordinator; they used to be orphaned because reason only
        dispatches its own decisions. When the reason cycle produces nothing
        fresh, preferring one of these turns the operator's direction into a
        claimable worker instead of a generic fallback.
        """
        if self.graph is None:
            return None
        try:
            rows = self.graph.open_operator_intents(limit=1)
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        stored_direction = sanitize_raw_direction(row.get("direction"))
        raw_direction = sanitize_raw_direction(row.get("raw_direction"))
        source_direction = raw_direction or stored_direction
        canonical_direction, resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(
            source_direction
        )
        if not canonical_direction and stored_direction:
            canonical_direction, resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                stored_direction
            )
        # Only a canonicalized operator value may enter the scheduler.  In
        # particular, never fall back to the raw stored value: an old or
        # externally-written invalid direction must not leak into decisions or
        # audit events.
        operator_selected = bool(canonical_direction)
        direction = canonical_direction or ""
        if not direction:
            category, category_resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                self.challenge.category
            )
            direction = category or self.challenge.category
            resolution = category_resolution if category else "category_fallback"
        return DispatchDecision(
            intent_id=str(row["intent_id"]),
            goal=str(row.get("goal") or ""),
            profile=direction_profile_name(direction) or "pi-worker",
            direction=direction,
            mode="explore",
            priority=float(row.get("priority") or 0.5),
            dedupe_key=f"operator:{row.get('directive_id') or row['intent_id']}",
            task_kind=self.challenge.category,
            host_scan=False,
            raw_direction=raw_direction,
            canonical_direction=direction,
            direction_resolution=resolution,
            direction_source="operator" if operator_selected else "category",
        )

    async def _run_worker(self, decision: DispatchDecision, profile: AgentProfile) -> Any:
        if self.worker_factory is None:
            raise RuntimeError("ReasonSwarm requires a worker_factory")
        lane = await self.lane_gate.acquire(
            mode=decision.mode,
            worker_class=decision.worker_class,
            stop_event=self.stop_event,
            pause_event=self.pause_event,
        )
        try:
            return await self.worker_factory(decision, profile)
        finally:
            self.lane_gate.release(lane)

    def _provider_diag_from_outcome(self, outcome: Any, error: Optional[str]) -> dict[str, Any]:
        diag = getattr(outcome, "provider_error", None) or {}
        if isinstance(diag, dict) and diag:
            return dict(diag)
        reason = str(getattr(outcome, "reason", "") or "")
        text = error or reason
        if not text:
            return {}
        low = text.lower()
        if "runtime failure" not in low and "provider" not in low:
            return {}
        return classify_provider_error(
            text,
            provider=str(getattr(outcome, "engine", "") or ""),
            worker_id=str(getattr(outcome, "solver_id", "") or ""),
        ).to_event()

    def _outcome_runtime_failed(self, outcome: Any, error: Optional[str],
                                provider_diag: dict[str, Any]) -> bool:
        if error is not None:
            return True
        if provider_diag:
            return True
        reason = str(getattr(outcome, "reason", "") or "").lower()
        return "runtime failure" in reason

    def _provider_diag_obj(self, diag: dict[str, Any]) -> ProviderErrorDiagnostic | None:
        if not diag:
            return None
        fields = {
            "category": str(diag.get("category") or "unknown_worker_failure"),
            "severity": str(diag.get("severity") or ("fatal" if diag.get("should_pause_dispatch") else "warning")),
            "retryable": bool(diag.get("retryable")),
            "should_pause_dispatch": bool(diag.get("should_pause_dispatch")),
            "provider": str(diag.get("provider") or ""),
            "account_id": str(diag.get("account_id") or ""),
            "worker_id": str(diag.get("worker_id") or ""),
            "raw_message": str(diag.get("raw_message") or "")[:1000],
            "user_message": str(diag.get("user_message") or "Worker provider/runtime error."),
            "suggested_action": str(diag.get("suggested_action") or "查看 worker/provider 配置并决定是否恢复。"),
        }
        return ProviderErrorDiagnostic(**fields)

    async def _record_provider_error(self, diag: dict[str, Any], *,
                                     active_workers: int) -> None:
        obj = self._provider_diag_obj(diag)
        if obj is None:
            return
        alert = self._provider_errors.record(
            obj, now=time.monotonic(), active_workers=active_workers)
        runtime = getattr(self.worker_factory, "__self__", None)
        swarm_ref = getattr(runtime, "swarm", None)
        if swarm_ref is not None and hasattr(swarm_ref, "trigger_role_fallback"):
            # Fatal account/model errors open the current override immediately;
            # retryable errors wait for the aggregator's same-window threshold.
            immediate_fatal = obj.severity == "fatal"
            if immediate_fatal or alert:
                await swarm_ref.trigger_role_fallback(
                    "worker",
                    reason=(f"Provider {obj.category}" if immediate_fatal else
                            f"Provider batch alert ({alert.get('count')} failures, {alert.get('affected_workers')} workers affected)"),
                    is_fatal=immediate_fatal,
                    failure_category=obj.category,
                    active_workers=active_workers,
                    affected_workers=(1 if immediate_fatal else int(alert.get("affected_workers", 0))),
                )
        if not alert:
            return
        if self.bus is not None:
            try:
                await self.bus.emit(Event(
                    event_type=EventType.PROVIDER_BATCH_ALERT,
                    run_id=self.run_id or self.challenge.id,
                    challenge_id=self.challenge.id,
                    payload=alert,
                ))
            except Exception:
                pass
        await self._emit(
            "provider_batch_alert",
            stage="dispatch",
            **{k: v for k, v in alert.items() if k != "type"},
        )

    async def _record_energy_cycle(
        self, fresh: list[DispatchDecision], cycle_id: str
    ) -> None:
        """M7 scheduler wiring step 2 (docs/10 v9.2): trace_id ->
        cycle_started -> bounded capture -> EnergyDecisions -> cycle_trace.
        Runs before _register_decision/dispatch; any failure only degrades the
        telemetry sample, never the solve."""
        run_id = self.run_id or self.challenge.id
        trace_id = energy.make_trace_id(
            run_id, self._instance_uuid, self._generation)
        decision_ts = time.time()
        self.energy_trace_sink.start_cycle(
            trace_id, reason_cycle_id=cycle_id, decision_ts=decision_ts)
        db_path = getattr(self.graph, "db_path", None)
        if db_path:
            snapshot = await asyncio.to_thread(
                energy_capture.capture_energy_cycle_snapshot,
                db_path, self.challenge.id)
        else:
            snapshot = energy.GraphCycleSnapshot(
                graph_after_seq=0, observations=(), dead_ends=(),
                complete=False, exclusion_reason="snapshot_unavailable",
                observed_fact_count=0, captured_fact_count=0,
                stored_fact_count=0)
        decisions = tuple(
            self._energy_decision(d, trace_id, cycle_id, index)
            for index, d in enumerate(fresh))
        trace = energy.CycleTrace(
            schema_version=energy.SCHEMA_VERSION,
            trace_id=trace_id,
            reason_cycle_id=cycle_id,
            decision_ts=decision_ts,
            expected_decision_count=len(fresh),
            decisions=decisions,
            snapshot=snapshot,
            complete=snapshot.complete and len(decisions) == len(fresh),
            exclusion_reason=snapshot.exclusion_reason,
            serialized_bytes=0,
            serialized_bytes_attempted=None,
        )
        self.energy_trace_sink.write_trace(trace)

    def _energy_decision(
        self, decision: DispatchDecision, trace_id: str, cycle_id: str,
        index: int,
    ) -> energy.EnergyDecision:
        """Field ownership is fully specified in docs/10 (no implementer
        guessing): original_index is the zero-based fresh index, worker_lane
        comes from lane_gate.lane_for, normalized_priority reuses
        priority.normalize_priority (no second implementation), priority_scale
        is frozen to "planner", decision_source to "reason"."""
        lane = self.lane_gate.lane_for(
            mode=decision.mode, worker_class=decision.worker_class)
        route_hash = ""
        if self.graph is not None:
            try:
                route_hash = self.graph.normalize_route_hash(
                    decision.canonical_direction or decision.direction or "")
            except Exception:
                route_hash = ""
        run_id = self.run_id or self.challenge.id
        return energy.EnergyDecision(
            decision_id=energy.decision_id_for(
                run_id, trace_id, index, decision.intent_id, "reason"),
            trace_id=trace_id,
            reason_cycle_id=cycle_id,
            intent_id=decision.intent_id,
            route_hash=route_hash,
            worker_lane=lane,
            priority=float(decision.priority),
            normalized_priority=normalize_priority(decision.priority),
            priority_scale="planner",
            original_index=index,
            decision_source="reason",
        )

    def _note_intent_registration_failure(
        self, decision: DispatchDecision, exc: BaseException
    ) -> None:
        """Surface a failed Reason intent write without blocking dispatch.

        ``propose_intent`` runs before a worker is launched, so dropping its
        exception can leave worker evidence orphaned in the shared graph.  Keep
        this diagnostic bounded and public-safe, and emit it asynchronously just
        like worker-side intent write diagnostics.
        """
        intent_id = str(getattr(decision, "intent_id", "") or "")
        if not intent_id or intent_id in self._intent_registration_failures_noted:
            return
        if len(self._intent_registration_failures_noted) >= 32:
            self._intent_registration_failures_noted.clear()
        self._intent_registration_failures_noted.add(intent_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._emit(
            "intent_db_write_failed",
            intent_id=intent_id,
            op="propose",
            reason=sanitize_public_text(type(exc).__name__, limit=160),
        ))

    @staticmethod
    def _quality_decision(value: Any) -> str:
        decision = str(value or "defer").strip().lower()
        return decision if decision in {"approved", "revise", "duplicate", "blocked", "defer"} else "defer"

    def _quality_replacement(
        self, original: DispatchDecision, raw: Any, *, cycle_id: str,
    ) -> Optional[DispatchDecision]:
        if not isinstance(raw, dict):
            return None
        goal = str(raw.get("goal") or "").strip()
        if not goal or (original.goal and len(goal) > max(len(original.goal) * 2, len(original.goal) + 120)):
            return None
        raw_from = raw.get("from_facts", raw.get("from", original.from_facts))
        if not isinstance(raw_from, list):
            return None
        try:
            from_facts = [int(x) for x in raw_from if int(x) > 0]
        except (TypeError, ValueError):
            return None
        if original.from_facts and not set(from_facts).issubset(set(original.from_facts)):
            return None
        worker_class = str(raw.get("worker_class") or original.worker_class).strip()
        if worker_class not in {"code", "shell_agent", "verifier", "review"}:
            return None
        replacement_id = str(raw.get("intent_id") or "").strip()
        if not replacement_id:
            suffix = str(self._generation) if getattr(self, "_generation", None) else str(cycle_id or "1")
            replacement_id = f"{original.intent_id}:rev:{suffix}"
        revised = DispatchDecision(
            intent_id=replacement_id,
            profile=str(raw.get("profile") or original.profile).strip() or original.profile,
            direction=original.direction,
            goal=goal,
            worker_class=worker_class,
            from_facts=from_facts,
            mode=str(raw.get("mode") or original.mode).strip() or original.mode,
            priority=float(raw.get("priority", original.priority) or original.priority),
            dedupe_key="",
            surface_target=original.surface_target,
            task_kind=original.task_kind,
            host_scan=original.host_scan,
            canonical_direction=original.canonical_direction,
            raw_direction=original.raw_direction,
            direction_resolution=original.direction_resolution,
            direction_source=original.direction_source,
            reproduction_id=original.reproduction_id,
            source_finding_id=original.source_finding_id,
            poc_id=original.poc_id,
            runtime_operation_kind=original.runtime_operation_kind,
        )
        revised.dedupe_key = _dedupe_key(revised)
        return revised

    async def _quality_gate(
        self, decision: DispatchDecision, *, reason_cycle_id: str,
    ) -> Optional[DispatchDecision]:
        # Recon and provider/runtime recovery are operational probes, not business
        # claims. They may run while the business Reviewer is unavailable.
        if decision.mode in {"recon", "recovery"} or decision.runtime_operation_kind == "recovery":
            return decision
        if self.intent_review_fn is None:
            if not self.require_reviewer:
                return decision
            # Production is fail-closed: an ordinary business intent cannot run
            # without an explicit Reviewer callback. Keep this distinct from a
            # worker/challenge failure so operators can repair the role wiring.
            reason = "reviewer unavailable"
            self._reviewer_available = False
            self._reviewer_blocked = True
            if self.graph is not None:
                try:
                    self.graph.decide_intent_quality(
                        actor="reviewer", intent_id=decision.intent_id,
                        decision="blocked", reason=reason,
                        reason_cycle_id=reason_cycle_id,
                    )
                except Exception:
                    pass
            await self._emit(
                "intent_quality_decision", stage="review",
                intent_id=decision.intent_id, decision="blocked", reason=reason,
                reason_cycle_id=reason_cycle_id, replacement_intent_id="",
                operator_attention=True,
            )
            await self._emit(
                "reviewer_quality_gate_blocked", stage="review",
                intent_id=decision.intent_id, reason=reason,
                reason_cycle_id=reason_cycle_id, error_type="reviewer_missing",
                operator_attention=True,
            )
            await self._emit(
                "reviewer_gate_blocked", stage="review",
                intent_id=decision.intent_id, reason=reason,
                reason_cycle_id=reason_cycle_id,
                operator_attention=True,
            )
            return None
        try:
            raw = await self.intent_review_fn(decision, reason_cycle_id)
            raw = raw if isinstance(raw, dict) else {}
            verdict = self._quality_decision(raw.get("decision"))
            reason = str(raw.get("reason") or "").strip()[:1000]
            replacement = raw.get("replacement")
            reviewed = self._quality_replacement(decision, replacement, cycle_id=reason_cycle_id)
            if verdict == "revise" and reviewed is None:
                verdict = "blocked"
                reason = (reason + "; " if reason else "") + "invalid narrower replacement"
            if self.graph is not None:
                self.graph.decide_intent_quality(
                    actor="reviewer", intent_id=decision.intent_id, decision=verdict,
                    reason=reason, replacement=(replacement if verdict == "revise" else None),
                    evidence_refs=[str(x) for x in (raw.get("evidence_refs") or []) if x],
                    reason_cycle_id=reason_cycle_id,
                )
            await self._emit(
                "intent_quality_decision", stage="review",
                intent_id=decision.intent_id, decision=verdict, reason=reason,
                reason_cycle_id=reason_cycle_id,
                replacement_intent_id=reviewed.intent_id if reviewed is not None else "",
            )
            if verdict == "approved":
                return decision
            if verdict == "revise" and reviewed is not None:
                await self._emit(
                    "intent_proposed", stage="review",
                    intent_id=reviewed.intent_id, original_intent_id=decision.intent_id,
                    proposal_kind="review_revision", goal=reviewed.goal,
                    worker_class=reviewed.worker_class, mode=reviewed.mode,
                    profile=reviewed.profile, from_facts=reviewed.from_facts,
                    dedupe_key=reviewed.dedupe_key, reason_cycle_id=reason_cycle_id,
                )
                return reviewed
            await self._emit(
                "intent_skipped", stage="review", intent_id=decision.intent_id,
                skip_reason=f"reviewer_{verdict}", reason=reason,
                reason_cycle_id=reason_cycle_id,
            )
            return None
        except Exception as exc:
            self._reviewer_available = False
            self._reviewer_blocked = True
            reason = "reviewer unavailable"
            diag = self._planner_exception_diag(exc)
            owner = getattr(self, "owner_swarm", None)
            if owner is not None:
                try:
                    await owner.trigger_role_fallback(
                        "review", reason=str(diag.get("detail") or reason),
                        is_fatal=False, failure_category=str(diag.get("code") or "reviewer_call_failed"),
                        active_workers=int(self.lane_gate.snapshot().get("ordinary_active", 0)),
                        # Only the Worker role aggregates a majority-of-active-workers
                        # ratio; Reason/Reviewer pass 0 so no ratio is fabricated.
                        affected_workers=0,
                    )
                except Exception:
                    pass
            if self.graph is not None:
                try:
                    self.graph.decide_intent_quality(
                        actor="reviewer", intent_id=decision.intent_id,
                        decision="blocked", reason=reason,
                        reason_cycle_id=reason_cycle_id,
                    )
                except Exception:
                    pass
            if not self._reviewer_failure_noted:
                self._reviewer_failure_noted = True
                await self._emit(
                    "intent_quality_decision", stage="review",
                    intent_id=decision.intent_id, decision="blocked", reason=reason,
                    reason_cycle_id=reason_cycle_id, replacement_intent_id="",
                    operator_attention=True,
                )
                await self._emit(
                    "reviewer_quality_gate_blocked", stage="review",
                    intent_id=decision.intent_id, reason=reason,
                    reason_cycle_id=reason_cycle_id,
                    error_type=type(exc).__name__, operator_attention=True,
                )
                await self._emit(
                    "reviewer_gate_blocked", stage="review",
                    intent_id=decision.intent_id, reason=reason,
                    reason_cycle_id=reason_cycle_id,
                    error_type=type(exc).__name__, operator_attention=True,
                )
            return None
    def _register_decision(self, decision: DispatchDecision) -> None:
        """Persist a Reason dispatch before the worker can produce graph products.

        ReasonSwarm used to emit an ``intent_proposed`` bus delta only.  The worker
        then wrote facts/POCs/conclusions referencing an intent that did not exist in
        SQLite.  Registration is best-effort so an unavailable graph never blocks a
        solve, but when a graph is present the intent row precedes worker execution.
        """
        if self.graph is None:
            return
        worker_class = decision.worker_class or (
            "review" if decision.mode == "review" else "shell_agent"
        )
        try:
            self.graph.propose_intent(
                actor="reason",
                intent_id=decision.intent_id,
                goal=decision.goal,
                payload={
                    "worker_class": worker_class,
                    "profile": decision.profile,
                    "direction": decision.direction,
                    "canonical_direction": decision.canonical_direction or decision.direction,
                    "raw_direction": decision.raw_direction,
                    "direction_resolution": decision.direction_resolution,
                    "direction_source": decision.direction_source,
                    "mode": decision.mode,
                    "priority": decision.priority,
                    "dedupe_key": decision.dedupe_key,
                    "surface_target": decision.surface_target,
                    "task_kind": decision.task_kind,
                    "host_scan": decision.host_scan,
                    "reproduction_id": decision.reproduction_id,
                    "source_finding_id": decision.source_finding_id,
                    "poc_id": decision.poc_id,
                },
                from_fact_seqs=decision.from_facts or None,
            )
        except Exception as exc:
            self._note_intent_registration_failure(decision, exc)

    def _board_summary(self) -> str:
        active = self.board.query_findings(FindingPredicate(limit=200))
        lines = [f"Challenge: {self.challenge.name} [{self.challenge.category}]"]
        for f in active:
            strength = f.pheromone()
            lines.append(
                f"- [{f.kind}] strength={strength:.2f} target={f.target} "
                f"payload={f.payload!r}"
            )
        return "\n".join(lines)

    def _planner_exception_diag(self, exc: BaseException) -> dict[str, Any]:
        try:
            from dswarm.core.llm import classify_llm_exception
            diag = classify_llm_exception(exc)
        except Exception:  # noqa: BLE001 - core scheduler must not depend on web
            diag = {
                "code": "planner_exception",
                "detail": str(exc) or type(exc).__name__,
            }
        out = dict(self._last_planner_error or {})
        code = str(diag.get("code") or "planner_exception")
        previous = str(out.get("code") or "")
        # A launch-time provider classification (e.g. missing_api_key from the
        # pre-run probe) is more specific than the generic network_error that a
        # bare "planner unavailable" RuntimeError classifies to. Keep it so the
        # fatal path (§5.4) and the operator diagnostic stay accurate.
        if previous and code in _GENERIC_PLANNER_CODES and previous not in _GENERIC_PLANNER_CODES:
            code = previous
            detail = str(out.get("detail") or diag.get("detail") or "")
        else:
            detail = str(diag.get("detail") or str(exc) or type(exc).__name__)
        out.update({
            "code": code,
            "detail": detail,
            "planner": str(out.get("planner") or self.reason_model),
        })
        return out

    async def _set_reason_health(self, state: str, *, reason_cycle_id: str = "", **fields: Any) -> None:
        """Project the host-owned Reason health state without changing scheduling.

        The emit is deduped on the health payload itself (not just the state):
        repeating an identical projection inside one cycle must not spam the
        event log, while a changed failure count still ticks through.
        """
        payload: dict[str, Any] = {
            "health_state": str(state or "healthy"),
            "failures": self._reason_cycle_failures,
            "max_failures": self._max_planner_failures,
            "request_failures": int(fields.pop("request_failures", 0) or 0),
            "request_limit": _REASON_REQUESTS_PER_CYCLE,
            "retry_window_s": _REASON_RETRY_WINDOW_S,
            # Normalise the optional flags so a semantically identical projection
            # is also structurally identical and can be deduped.
            "operator_attention": bool(fields.pop("operator_attention", False)),
            "immediate": bool(fields.pop("immediate", False)),
        }
        payload.update(fields)
        self._reason_health_state = payload["health_state"]
        if payload == self._reason_health_payload:
            return
        self._reason_health_payload = payload
        await self._emit(
            "reason_health_changed",
            stage="reason",
            reason_cycle_id=reason_cycle_id,
            **payload,
        )

    async def _run_reason(self, *, reason_cycle_id: str = "") -> ReasonResult:
        if self.graph is not None:
            try:
                summary = self.graph.to_reason_summary()
            except Exception:
                summary = self._board_summary()
        else:
            summary = self._board_summary()
        result: ReasonResult | None = None
        last_exc: BaseException | None = None
        request_failures = 0
        if self.reason_fn is not None:
            for attempt in range(_REASON_REQUESTS_PER_CYCLE):
                try:
                    result = await self.reason_fn(summary, self.challenge.id)
                    self._last_planner_error = {}
                    break
                except Exception as exc:  # noqa: BLE001 — planner is advisory
                    last_exc = exc
                    request_failures += 1
                    if request_failures == 1:
                        await self._set_reason_health(
                            "degraded",
                            reason_cycle_id=reason_cycle_id,
                            request_failures=request_failures,
                        )
                    elif request_failures == _REASON_REQUESTS_PER_CYCLE:
                        await self._set_reason_health(
                            "retrying",
                            reason_cycle_id=reason_cycle_id,
                            request_failures=request_failures,
                            operator_attention=True,
                        )
                    if attempt < _REASON_REQUESTS_PER_CYCLE - 1:
                        await asyncio.sleep(2.0 * (attempt + 1))
        elif self.llm is not None:
            from dswarm.solver.reason import run_reason
            for attempt in range(_REASON_REQUESTS_PER_CYCLE):
                try:
                    result = await run_reason(
                        llm=self.llm, model=self.reason_model,
                        graph_summary=summary, run_id=self.challenge.id,
                        challenge_id=self.challenge.id,
                        mode=getattr(self.challenge, "mode", "ctf"),
                        goal=getattr(self.challenge, "goal", "") or None,
                    )
                    self._last_planner_error = {}
                    break
                except Exception as exc:  # noqa: BLE001 — planner is advisory
                    last_exc = exc
                    request_failures += 1
                    if request_failures == 1:
                        await self._set_reason_health(
                            "degraded",
                            reason_cycle_id=reason_cycle_id,
                            request_failures=request_failures,
                        )
                    elif request_failures == _REASON_REQUESTS_PER_CYCLE:
                        await self._set_reason_health(
                            "retrying",
                            reason_cycle_id=reason_cycle_id,
                            request_failures=request_failures,
                            operator_attention=True,
                        )
                    if attempt < _REASON_REQUESTS_PER_CYCLE - 1:
                        await asyncio.sleep(2.0 * (attempt + 1))
        elif self._last_planner_error and self._last_planner_error.get("code") != "ok":
            # A pre-run launch diagnostic (no callable planner). No request was
            # attempted, so request_failures stays 0; the cycle-level block below
            # reports the single honest `degraded` fact.
            last_exc = RuntimeError("planner unavailable")

        if result is None:
            if last_exc is not None:
                self._last_planner_error = self._planner_exception_diag(last_exc)
            self._reason_cycle_failures += 1
            code = str((self._last_planner_error or {}).get("code") or "")
            # docs/17 §5.4: quota/auth/model-not-found need no six-cycle wait —
            # the first occurrence already opens this override's circuit. The
            # host still probes the settings default before switching.
            immediate = code in _IMMEDIATE_CIRCUIT_CODES
            if immediate or self._reason_cycle_failures >= self._max_planner_failures:
                health = "circuit_open"
            elif request_failures:
                # Three failed requests inside this cycle (the loop above already
                # announced the request-level transition).
                health = "retrying"
            else:
                # The planner was never callable this cycle (launch diagnostic
                # only) — that is a single degraded fact, not a retry storm.
                health = "degraded"
            self._reason_circuit_open = health == "circuit_open"
            await self._set_reason_health(
                health,
                reason_cycle_id=reason_cycle_id,
                request_failures=request_failures,
                operator_attention=health in {"retrying", "circuit_open"},
                immediate=immediate,
            )
            result = ReasonResult(
                goal_met=False, intents=[], audit_notes=["reason planner unavailable"])
        else:
            had_failures = self._reason_cycle_failures or request_failures
            self._reason_cycle_failures = 0
            self._reason_circuit_open = False
            if had_failures:
                await self._set_reason_health(
                    "healthy", reason_cycle_id=reason_cycle_id, request_failures=0)

        if self.owner_swarm is not None:
            current_client = getattr(self.owner_swarm, "llm", None)
            if current_client is not None:
                self.llm = current_client
        self._last_reason = result
        return result

    def _decisions_from_reason(self, result: ReasonResult) -> list[DispatchDecision]:
        """Resolve intents using the M4.1 authority chain.

        ``direction_resolution`` preserves what the model declared and how it
        parsed. ``direction_source`` records the authority that supplied the
        final dispatch direction. Operator direction is CTF-only and never
        overrides a valid model direction.
        """
        out: list[DispatchDecision] = []
        operator_direction = ""
        if getattr(self.challenge, "mode", "ctf") == "ctf":
            operator_direction, _ = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                getattr(self.challenge, "direction", "")
            )

        for it in result.intents:
            mode = it.mode or ("recon" if it.requires_recon else "explore")
            raw_direction = sanitize_raw_direction(it.raw_direction)
            declared_direction = sanitize_raw_direction(
                it.direction or it.canonical_direction
            )
            source_direction = raw_direction or declared_direction
            model_direction, model_resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                source_direction
            )

            # A raw direction and an explicitly supplied canonical direction are
            # both diagnostics. If they disagree, do not silently choose one.
            declared_canonical = sanitize_raw_direction(it.canonical_direction)
            if raw_direction and declared_canonical:
                declared_value, _ = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                    declared_canonical
                )
                if (declared_value and model_direction
                        and declared_value != model_direction):
                    model_direction = ""
                    model_resolution = "invalid"

            direction = model_direction
            direction_source = "model" if model_direction else ""
            resolution = model_resolution
            override_reason = ""

            if not model_direction and model_resolution in {
                "empty", "explicit_auto", "invalid"
            }:
                if operator_direction:
                    direction = operator_direction
                    direction_source = "operator"
                    override_reason = {
                        "empty": "model_direction_empty",
                        "explicit_auto": "model_direction_explicit_auto",
                        "invalid": "model_direction_invalid",
                    }[model_resolution]
                else:
                    suggested = DEFAULT_DIRECTION_REGISTRY.suggest(
                        it.goal, getattr(it, "rationale", "")
                    )
                    if suggested is not None:
                        direction, resolution = suggested
                        direction_source = "keyword"
                        override_reason = "keyword_fallback"
                    else:
                        category_direction, _ = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                            self.challenge.category
                        )
                        direction = category_direction or sanitize_raw_direction(
                            self.challenge.category
                        )
                        direction_source = "category" if direction else "default"
                        resolution = "category_fallback"
                        override_reason = "category_fallback"

            if not direction:
                direction = ""
                direction_source = "default"

            profile_id = (
                DEFAULT_DIRECTION_REGISTRY.profile_for(direction)
                or direction_profile_name(direction)
                or it.profile
                or direction_profile_name(self.challenge.category)
                or "pi-worker"
            )
            decision = DispatchDecision(
                intent_id=it.intent_id,
                profile=profile_id,
                direction=direction,
                goal=it.goal,
                worker_class=it.worker_class,
                from_facts=it.from_facts,
                mode=mode,
                priority=it.priority,
                dedupe_key=_dedupe_key(
                    DispatchDecision(
                        intent_id=it.intent_id,
                        profile=profile_id,
                        goal=it.goal,
                        mode=mode,
                    )
                ),
                surface_target=it.surface_target,
                task_kind=it.task_kind or self.challenge.category,
                host_scan=it.host_scan,
                raw_direction=raw_direction,
                canonical_direction=direction,
                direction_resolution=resolution,
                direction_source=direction_source,
                reproduction_id=it.reproduction_id,
                source_finding_id=it.source_finding_id,
                poc_id=getattr(it, "poc_id", ""),
            )
            out.append(decision)

            # Emit only when the effective route differs from the model's
            # dispatchable declaration. A valid model direction never emits an
            # override, even when an operator direction is present.
            if direction != model_direction and override_reason:
                self._direction_overrides.append({
                    "intent_id": decision.intent_id,
                    "goal": decision.goal,
                    "raw_direction": raw_direction,
                    "model_direction": model_direction,
                    "previous_direction": declared_direction,
                    "operator_direction": operator_direction,
                    "canonical_direction": direction,
                    "direction": direction,
                    "direction_resolution": model_resolution,
                    "direction_source": direction_source,
                    "reason": override_reason,
                })
        return out
    async def _reap_abandoned_claims(self, *, reason_cycle_id: str) -> None:
        """Park claims whose lease expired without a conclusion (docs/17 §9.2).

        Without this an abandoned claim was silently re-dispatched by
        ``dispatchable_intents`` — the Intent never entered the documented
        ``awaiting_validation`` state, so the UI could not show "worker interrupted /
        no real feedback" and no recovery audit existed. Parking here converts it
        into a bounded recovery item; the caller reopens it in the same cycle.
        """
        if self.graph is None or not hasattr(self.graph, "park_expired_claims"):
            return
        try:
            parked = self.graph.park_expired_claims(
                actor="reason", reason="claim_lease_expired",
                limit=self.max_intents_per_reason,
            )
        except Exception:
            return
        for row in parked:
            await self._emit(
                "intent_interrupted", stage="recovery",
                intent_id=str(row.get("intent_id") or ""),
                worker=str(row.get("worker") or ""),
                dispatch_state="awaiting_validation",
                reason_cycle_id=reason_cycle_id,
                reason="claim_lease_expired",
                operator_attention=False,
            )

    async def _recover_awaiting_validation(self, *, reason_cycle_id: str) -> list[DispatchDecision]:
        """Reopen bounded no-feedback interruptions before new business Intents."""
        if self.graph is None or not hasattr(self.graph, "awaiting_validation_intents"):
            return []
        try:
            parked = self.graph.awaiting_validation_intents(limit=self.max_intents_per_reason)
        except Exception:
            return []
        out: list[DispatchDecision] = []
        for item in parked:
            intent_id = str(item.get("intent_id") or "").strip()
            if not intent_id:
                continue
            recovery_count = 0
            try:
                for event in self.graph.events():
                    if event.get("kind") != "intent_recovery_required":
                        continue
                    payload = event.get("payload") or {}
                    if str(payload.get("intent_id") or "") == intent_id:
                        recovery_count = max(recovery_count, int(payload.get("recovery_attempt") or 0))
            except Exception:
                pass
            if recovery_count >= self._max_recovery_attempts:
                await self._emit(
                    "intent_recovery_exhausted", stage="recovery",
                    intent_id=intent_id, reason_cycle_id=reason_cycle_id,
                    recovery_attempt=recovery_count, operator_attention=True,
                )
                continue
            recovery_intent_id = f"{intent_id}:recovery:{recovery_count + 1}"
            try:
                self.graph.reopen_interrupted_intent(
                    actor="reason", intent_id=intent_id,
                    recovery_intent_id=recovery_intent_id,
                    worker_id=str(item.get("worker") or ""),
                    reason="Reason recovery priority",
                )
            except Exception as exc:
                await self._emit(
                    "intent_recovery_blocked", stage="recovery",
                    intent_id=intent_id, reason_cycle_id=reason_cycle_id,
                    error_type=type(exc).__name__, operator_attention=True,
                )
                continue
            await self._emit(
                "intent_recovery_required", stage="recovery",
                intent_id=intent_id, recovery_intent_id=recovery_intent_id,
                reason_cycle_id=reason_cycle_id, recovery_attempt=recovery_count + 1,
            )
            goal = str(item.get("goal") or f"Recover interrupted intent {intent_id}")
            direction = str(item.get("direction") or "")
            profile_id = (
                DEFAULT_DIRECTION_REGISTRY.profile_for(direction)
                or direction_profile_name(direction)
                or direction_profile_name(self.challenge.category)
                or "pi-worker"
            )
            decision = DispatchDecision(
                intent_id=recovery_intent_id,
                profile=profile_id,
                direction=direction,
                canonical_direction=direction,
                direction_source="recovery",
                goal=goal,
                worker_class=str(item.get("worker_class") or "code"),
                mode="explore",
                priority=float(item.get("priority") or 1.0) + 1.0,
                dedupe_key=f"recovery:{recovery_intent_id}",
                task_kind=str(item.get("task_kind") or self.challenge.category),
                runtime_operation_kind="recovery",
            )
            out.append(decision)
        return out
    def _flags_complete(self) -> bool:
        expected = max(1, getattr(self.challenge, "expected_flags", 1) or 1)
        multi = bool(getattr(self.challenge, "multi_flag", False))
        flags = self.board.query_findings(
            FindingPredicate(kinds=(FindingKind.FLAG_FOUND,))
        )
        if multi and expected <= 1:
            return False
        return len(flags) >= expected

    def _budget_exceeded(self) -> bool:
        budget = self.board.budget(self.challenge.id)
        if (
            budget.get("max_agent_hours", 0) > 0
            and budget.get("used_hours", 0) >= budget["max_agent_hours"]
        ):
            return True
        if (
            budget.get("max_tokens", 0) > 0
            and budget.get("used_tokens", 0) >= budget["max_tokens"]
        ):
            return True
        return False

    async def _write_initial_target(self) -> None:
        existing = self.board.query_findings(
            FindingPredicate(kinds=(FindingKind.TARGET_REGISTERED,), limit=1)
        )
        if existing:
            return
        self.board.write_finding(
            challenge_id=self.challenge.id,
            kind=FindingKind.TARGET_REGISTERED,
            agent_name="engine",
            target=self.challenge.target or self.challenge.name,
            payload={"name": self.challenge.name, "category": self.challenge.category},
        )

    async def run(self) -> Any:
        t0 = time.monotonic()
        # The reason for leaving the planning loop. Every `break` that is not a
        # normal completion records it here so the tail emits exactly ONE
        # authoritative reason_loop_finished event.
        terminal_reason = ""
        await self._write_initial_target()
        if self.graph is not None and self.projector is not None:
            try:
                self.projector.sync(self.graph)
            except Exception:
                pass

        recon_goal = (
            f"Initial recon of {self.challenge.name} [{self.challenge.category}] "
            "and full attack-surface mapping."
        )
        operator_direction = ""
        if getattr(self.challenge, "mode", "ctf") == "ctf":
            operator_direction, _ = DEFAULT_DIRECTION_REGISTRY.canonicalize(
                getattr(self.challenge, "direction", "")
            )
        category_direction, _ = DEFAULT_DIRECTION_REGISTRY.canonicalize(
            self.challenge.category
        )
        recon_direction = operator_direction or category_direction or ""
        recon_source = "operator" if operator_direction else (
            "category" if category_direction else "default"
        )
        recon_profile = self.agents.resolve(
            DEFAULT_DIRECTION_REGISTRY.profile_for(recon_direction)
            or direction_profile_name(recon_direction)
            or "pi-worker"
        )
        recon_decision = DispatchDecision(
            intent_id="recon-initial",
            profile=recon_profile.id,
            direction=recon_direction,
            canonical_direction=recon_direction,
            direction_source=recon_source,
            goal=recon_goal,
            mode="recon",
            priority=1.0,
            dedupe_key="recon:recon:initial",
            task_kind=self.challenge.category,
            runtime_operation_kind=self.initial_runtime_operation_kind,
        )
        findings_before_recon = len(
            self.board.query_findings(FindingPredicate(limit=10000))
        )
        self._register_decision(recon_decision)
        await self._emit(
            "recon_started",
            stage="recon",
            intent_id=recon_decision.intent_id,
            goal=recon_goal,
            profile=recon_profile.id,
            direction=recon_decision.direction,
            direction_source=recon_decision.direction_source,
            task_kind=recon_decision.task_kind,
        )
        t1 = time.monotonic()
        try:
            recon_outcome = await self._run_worker(recon_decision, recon_profile)
        except Exception:
            recon_outcome = self._salvage_outcome()
        self._absorb_outcome(recon_outcome)
        findings_after_recon = len(
            self.board.query_findings(FindingPredicate(limit=10000))
        )
        await self._emit(
            "recon_completed",
            stage="recon",
            intent_id=recon_decision.intent_id,
            duration_ms=int((time.monotonic() - t1) * 1000),
            new_findings=max(0, findings_after_recon - findings_before_recon),
            flag=getattr(recon_outcome, "flag", None),
            flags=len(getattr(recon_outcome, "flags", None) or []),
        )
        if self.graph is not None and self.projector is not None:
            try:
                self.projector.sync(self.graph)
            except Exception:
                pass

        while not self._flags_complete():
            if (
                time.monotonic() - t0 > self.wall_clock_budget
                or self._budget_exceeded()
                or (self.stop_event is not None and self.stop_event.is_set())
            ):
                break
            if self.pause_event is not None and not self.pause_event.is_set():
                await self._emit("operator_paused", stage="reason")
                await self.pause_event.wait()
            self._generation += 1
            cycle_id = f"reason-{self._generation}"
            await self._emit(
                "reason_cycle_started",
                stage="reason",
                reason_cycle_id=cycle_id,
                generation=self._generation,
                lineage_from=f"run:{self.run_id or self.challenge.id}",
                lineage_to=f"reason:{cycle_id}",
            )
            tc = time.monotonic()
            result: Optional[ReasonResult] = None
            try:
                if self.evidence_review_fn is not None:
                    try:
                        await self.evidence_review_fn()
                    except Exception as exc:
                        await self._emit(
                            "reviewer_evidence_gate_blocked", stage="review",
                            reason="evidence_review_failed", reason_cycle_id=cycle_id,
                            error_type=type(exc).__name__, operator_attention=True,
                        )
                await self._reap_abandoned_claims(reason_cycle_id=cycle_id)
                recovery_decisions = await self._recover_awaiting_validation(reason_cycle_id=cycle_id)
                result = await self._run_reason(reason_cycle_id=cycle_id)
                goal_claimed = bool(getattr(result, "goal_met", False))
                goal_accepted = goal_claimed and self._flags_complete()
                if goal_claimed and not goal_accepted:
                    await self._emit(
                        "reason_goal_claim_rejected",
                        stage="reason",
                        reason_cycle_id=cycle_id,
                        reason="provenance_gated_flag_required",
                    )
                if goal_accepted:
                    break
                override_start = len(self._direction_overrides)
                original_decisions = self._decisions_from_reason(result)
                decisions: list[DispatchDecision] = list(recovery_decisions)
                for decision in original_decisions:
                    await self._emit(
                        "intent_proposed", stage="reason",
                        intent_id=decision.intent_id, goal=decision.goal,
                        mode=decision.mode, priority=decision.priority,
                        profile=decision.profile, direction=decision.direction,
                        canonical_direction=decision.canonical_direction,
                        raw_direction=decision.raw_direction,
                        direction_resolution=decision.direction_resolution,
                        from_facts=decision.from_facts, dedupe_key=decision.dedupe_key,
                        reason_cycle_id=cycle_id,
                        lineage_from=f"reason:{cycle_id}",
                        lineage_to=f"intent:{decision.intent_id}",
                    )
                    reviewed = await self._quality_gate(decision, reason_cycle_id=cycle_id)
                    if reviewed is not None:
                        decisions.append(reviewed)
                for override in self._direction_overrides[override_start:]:
                    await self._emit("direction_override", stage="reason", **override)

                async def _one(decision: DispatchDecision) -> None:
                    profile = self.agents.resolve(decision.profile)
                    t2 = time.monotonic()
                    error: Optional[str] = None
                    try:
                        outcome = await self._run_worker(decision, profile)
                    except Exception as exc:
                        error = str(exc) or type(exc).__name__
                        outcome = self._salvage_outcome()
                    elapsed_hours = (time.monotonic() - t2) / 3600
                    self.board.update_budget(self.challenge.id, delta_hours=elapsed_hours, delta_tokens=0)
                    self.board.charge_agent(self.challenge.id, profile.id, tokens=0)
                    self._absorb_outcome(outcome)
                    if self.graph is not None and self.projector is not None:
                        try:
                            self.projector.sync(self.graph)
                        except Exception:
                            pass
                    provider_diag = self._provider_diag_from_outcome(outcome, error)
                    lifecycle_reason = str(getattr(outcome, "lifecycle_reason", "") or "")
                    raw_runtime_status = getattr(outcome, "runtime_status", {}) or {}
                    runtime_status = raw_runtime_status if isinstance(raw_runtime_status, dict) else {}
                    runtime_status = {
                        str(key): value for key, value in runtime_status.items()
                        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
                    }
                    interrupted = bool(getattr(outcome, "no_output_interruption", False))
                    feedback = getattr(outcome, "feedback", {}) or {}
                    meaningful_feedback = bool(feedback.get("meaningful")) if isinstance(feedback, dict) else False
                    lifecycle_runtime_failure = lifecycle_reason in {"oom", "stalled", "timeout"}
                    runtime_failed = self._outcome_runtime_failed(outcome, error, provider_diag)
                    runtime_failed = runtime_failed or (lifecycle_runtime_failure and not interrupted)
                    fields: dict[str, Any] = {
                        "stage": "execute", "intent_id": decision.intent_id,
                        "profile": profile.id, "mode": decision.mode,
                        "reason_cycle_id": cycle_id,
                        "duration_ms": int((time.monotonic() - t2) * 1000),
                        "flag": getattr(outcome, "flag", None),
                        "dispatch_state": "awaiting_validation" if interrupted else "",
                        "lifecycle_reason": lifecycle_reason,
                        "runtime_status": runtime_status,
                        "no_feedback": bool(interrupted and not meaningful_feedback),
                    }
                    if error is not None:
                        fields["error"] = error
                    elif runtime_failed:
                        fields["error"] = (str(provider_diag.get("raw_message") or "")
                                            or str(getattr(outcome, "reason", "") or "runtime failure"))
                    if provider_diag:
                        fields["provider_error"] = provider_diag
                        active = self.lane_gate.snapshot().get("ordinary_active", 0)
                        await self._record_provider_error(provider_diag, active_workers=max(1, int(active)))
                    if interrupted:
                        await self._emit("intent_interrupted", **fields)
                        await self._emit("intent_recovery_required", **fields)
                    elif runtime_failed and provider_diag.get("retryable"):
                        attempts = self._recovery_attempts.get(decision.dedupe_key, 0) + 1
                        self._recovery_attempts[decision.dedupe_key] = attempts
                        if attempts <= self._max_recovery_attempts:
                            self._executed.discard(decision.dedupe_key)
                            self._recovery_pending.add(decision.dedupe_key)
                            await self._emit("worker_recovery_scheduled", stage="dispatch",
                                              intent_id=decision.intent_id, profile=profile.id,
                                              mode=decision.mode, dedupe_key=decision.dedupe_key,
                                              reason_cycle_id=cycle_id, recovery_action="redispatch_intent",
                                              attempts=attempts, max_attempts=self._max_recovery_attempts,
                                              current_worker_interrupted=False, provider_error=provider_diag,
                                              lifecycle_reason=lifecycle_reason, runtime_status=runtime_status)
                        else:
                            await self._emit("worker_recovery_exhausted", stage="dispatch",
                                              intent_id=decision.intent_id, profile=profile.id,
                                              dedupe_key=decision.dedupe_key, attempts=attempts,
                                              max_attempts=self._max_recovery_attempts,
                                              provider_error=provider_diag, lifecycle_reason=lifecycle_reason,
                                              runtime_status=runtime_status)
                    elif runtime_failed and provider_diag.get("should_pause_dispatch"):
                        self._paused_profiles.add(profile.id)
                        await self._emit("provider_dispatch_paused", stage="dispatch",
                                          intent_id=decision.intent_id, profile=profile.id,
                                          reason_cycle_id=cycle_id, provider_error=provider_diag)
                    if not interrupted:
                        await self._emit("intent_failed" if runtime_failed else "intent_completed", **fields)

                capped = decisions[: self.max_intents_per_reason]
                fresh = [
                    replace(d, runtime_operation_kind="recovery")
                    if d.dedupe_key in self._recovery_pending else d
                    for d in capped
                    if d.dedupe_key not in self._executed
                    and d.profile not in self._paused_profiles
                ]

                for d in capped:
                    if d.profile in self._paused_profiles:
                        await self._emit(
                            "intent_skipped",
                            stage="dispatch",
                            intent_id=d.intent_id,
                            dedupe_key=d.dedupe_key,
                            profile=d.profile,
                            skip_reason="provider_dispatch_paused",
                            reason_cycle_id=cycle_id,
                        )
                    elif d.dedupe_key in self._executed:
                        await self._emit(
                            "intent_skipped",
                            stage="dispatch",
                            intent_id=d.intent_id,
                            dedupe_key=d.dedupe_key,
                            skip_reason="duplicate",
                            reason_cycle_id=cycle_id,
                        )
                if not fresh:
                    # run-3155: a transient planner outage must NOT be treated as
                    # "no fresh intents" — that collapsed the run (fallback →
                    # next failed cycle → break → finished) on a provider hiccup.
                    # Retry the next cycle with backoff instead; only give up after
                    # a bounded run of consecutive failures.
                    planner_unavailable = any(
                        str(n).startswith("reason planner unavailable")
                        for n in (getattr(result, "audit_notes", None) or []))
                    # expected_flags>1 keeps the run in flag-collection mode: until
                    # _flags_complete(), a collected flag is NOT a stop signal, and
                    # the Worker role is independent of the planner. A planner
                    # outage must therefore never end a multi-flag run that still
                    # needs flags — it only bounds the planner's own retries.
                    multi_collect = max(
                        1, int(getattr(self.challenge, "expected_flags", 1) or 1)) > 1
                    if planner_unavailable and not self._flags_complete():
                        self._planner_failures += 1
                        planner_diag = dict(self._last_planner_error or {})
                        planner_diag.setdefault("code", "planner_unavailable")
                        planner_diag.setdefault("detail", "Reason planner unavailable.")
                        planner_diag.setdefault("planner", self.reason_model)
                        owner = self.owner_swarm
                        fallback_activated = False
                        fallback_blocked = False
                        if owner is not None:
                            code = str(planner_diag.get("code") or "planner_unavailable")
                            try:
                                fallback_activated = await owner.trigger_role_fallback(
                                    "reason", reason=str(planner_diag.get("detail") or "Reason planner unavailable."),
                                    # docs/17 §5.3/§5.4: quota/auth/model-not-found (and a
                                    # bad endpoint) mark the override unusable at once and
                                    # still probe the settings default before switching.
                                    is_fatal=code in _IMMEDIATE_CIRCUIT_CODES,
                                    failure_category=code,
                                    active_workers=int(self.lane_gate.snapshot().get("ordinary_active", 0)),
                                    # Only the Worker role aggregates a
                                    # majority-of-active-workers ratio.
                                    affected_workers=0,
                                )
                                fallback_blocked = "reason" in getattr(owner, "_role_fallback_blocked", {})
                                if fallback_blocked:
                                    fallback_activated = False
                            except Exception:
                                fallback_activated = False  # K: recovery telemetry must not stop scheduler
                        await self._emit(
                            "reason_planner_unavailable", stage="reason", reason_cycle_id=cycle_id,
                            failures=self._planner_failures, max_failures=self._max_planner_failures,
                            fallback_active=fallback_activated, blocked=fallback_blocked,
                            operator_attention=fallback_blocked, **planner_diag,
                        )
                        if fallback_blocked:
                            await self._set_reason_health(
                                "waiting_for_operator", reason_cycle_id=cycle_id,
                                operator_attention=True,
                                blocked_reason="default_provider_probe_failed",
                            )
                            await self._emit(
                                "reason_blocked", stage="reason", reason_cycle_id=cycle_id,
                                reason="default_provider_probe_failed", operator_attention=True,
                            )
                        if fallback_activated:
                            # The effective binding changed, so the broken
                            # override's failure streak no longer describes this
                            # role. Keeping it would mark a recovered Reason as
                            # blocked on the next cycle (docs/17 §5.4).
                            self._planner_failures = 0
                            self._reason_cycle_failures = 0
                            self._reason_circuit_open = False
                            await self._set_reason_health(
                                "healthy", reason_cycle_id=cycle_id,
                                request_failures=0, fallback_active=True,
                            )
                        if (self._planner_failures >= self._max_planner_failures
                                and not multi_collect):
                            await self._set_reason_health(
                                "reason_blocked", reason_cycle_id=cycle_id,
                                operator_attention=True,
                                blocked_reason="planner_unavailable",
                            )
                            await self._emit(
                                "reason_blocked", stage="reason", reason_cycle_id=cycle_id,
                                reason="planner_unavailable", operator_attention=True,
                            )
                            # The single authoritative loop-finished event is
                            # emitted by the tail so the stop_reason can never
                            # contradict this break.
                            terminal_reason = "planner_unavailable"
                            break
                        if not multi_collect:
                            # A planner outage is not an empty business plan. Keep the
                            # run in the bounded retry loop; do not synthesize or dispatch
                            # fallback-bootstrap work from this cycle.
                            continue
                        # Multi-flag: fall through to the Worker flag-collection
                        # fallback below. The outage stays recorded (no fabricated
                        # recovery) while a healthy Worker role collects the rest.
                    else:
                        was_planner_blocked = self._planner_failures > 0
                        self._planner_failures = 0
                        if was_planner_blocked:
                            await self._emit(
                                "reason_recovered", stage="reason",
                                reason_cycle_id=cycle_id,
                                reason="planner_cycle_succeeded",
                                operator_attention=False,
                            )
                    self._reviewer_blocked = False
                    self._reviewer_available = self.intent_review_fn is not None
                    if not self._flags_complete():
                        operator = self._open_operator_decision()
                        if (operator is not None
                                and operator.dedupe_key not in self._executed):
                            reviewed_op = await self._quality_gate(operator, reason_cycle_id=cycle_id)
                            if reviewed_op is not None:
                                self._executed.add(operator.dedupe_key)
                                await self._emit(
                                    "dispatch_decision",
                                    stage="dispatch",
                                    intent_id=reviewed_op.intent_id,
                                    profile=reviewed_op.profile,
                                    priority=reviewed_op.priority,
                                    reason_cycle_id=cycle_id,
                                    dispatch_reason="operator directive intent",
                                )
                                await _one(reviewed_op)
                                continue
                    collected_flags = len(self.board.query_findings(
                        FindingPredicate(kinds=(FindingKind.FLAG_FOUND,))))
                    if not self._flags_complete() and (multi_collect or not self._fallback_executed):
                        if not multi_collect:
                            self._fallback_executed = True
                        fallback_round = collected_flags + 1 if multi_collect else 1
                        fallback = DispatchDecision(
                            intent_id=("fallback-bootstrap" if not multi_collect
                                       else f"fallback-bootstrap:{fallback_round}"),
                            profile=(direction_profile_name(self.challenge.category) or "pi-worker"),
                            goal=(
                                f"Continue solving {self.challenge.name} "
                                f"[{self.challenge.category}]; exploit the confirmed "
                                "surface and recover the flag."
                            ),
                            mode="bootstrap",
                            priority=1.0,
                            dedupe_key=("fallback:bootstrap" if not multi_collect
                                        else f"fallback:bootstrap:{fallback_round}"),
                            task_kind=self.challenge.category,
                            host_scan=False,
                        )
                        reviewed_fallback = await self._quality_gate(fallback, reason_cycle_id=cycle_id)
                        if reviewed_fallback is not None:
                            self._register_decision(reviewed_fallback)
                            await self._emit(
                                "fallback_dispatch",
                                stage="dispatch",
                                intent_id=reviewed_fallback.intent_id,
                                reason="no fresh intents",
                                reason_cycle_id=cycle_id,
                            )
                            await _one(reviewed_fallback)
                            continue
                        await self._emit(
                            "fallback_dispatch_blocked",
                            stage="dispatch",
                            intent_id=fallback.intent_id,
                            reason="reviewer rejected fallback bootstrap",
                            reason_cycle_id=cycle_id,
                        )
                    break
                # M7 offline energy telemetry (docs/10 scheduler order step 2):
                # after fresh is formed, BEFORE _register_decision/dispatch.
                # cycle_started -> capture -> cycle_trace; sample failures never
                # break the reason loop or reorder dispatch.
                if self.energy_trace_enabled and self.energy_trace_sink is not None:
                    try:
                        await self._record_energy_cycle(fresh, cycle_id)
                    except Exception:
                        pass  # telemetry must never break the solve
                for decision in fresh:
                    self._recovery_pending.discard(decision.dedupe_key)
                    self._executed.add(decision.dedupe_key)
                    self._register_decision(decision)
                    await self._emit(
                        "dispatch_decision",
                        stage="dispatch",
                        intent_id=decision.intent_id,
                        profile=decision.profile,
                        priority=decision.priority,
                        reason_cycle_id=cycle_id,
                        dispatch_reason="highest-priority unclaimed intent",
                    )

                await asyncio.gather(*[_one(d) for d in fresh], return_exceptions=True)
            finally:
                await self._emit(
                    "reason_cycle_completed",
                    stage="reason",
                    reason_cycle_id=cycle_id,
                    generation=self._generation,
                    duration_ms=int((time.monotonic() - tc) * 1000),
                    audit_notes=list(getattr(result, "audit_notes", None) or []),
                    goal_met=bool(getattr(result, "goal_met", False)),
                    goal_met_accepted=bool(
                        getattr(result, "goal_met", False) and self._flags_complete()
                    ),
                    planner=self.reason_model,
                )

        if terminal_reason:
            stop_reason = terminal_reason
        elif self.stop_event is not None and self.stop_event.is_set():
            stop_reason = "operator_stop"
        elif self._flags_complete():
            stop_reason = "goal_met"
        elif time.monotonic() - t0 > self.wall_clock_budget or self._budget_exceeded():
            stop_reason = "budget_exceeded"
        else:
            stop_reason = "no_fresh_intents"
        await self._emit(
            "reason_loop_finished",
            stage="finalize",
            stop_reason=stop_reason,
            solved=self._flags_complete(),
            generations=self._generation,
        )
        return {
            "solved": self._flags_complete(),
            "flags": [
                f.payload.get("flag") or f.target
                for f in self.board.query_findings(
                    FindingPredicate(kinds=(FindingKind.FLAG_FOUND,))
                )
            ],
            "winner_outcome": self._winner_outcome,
        }

    def _salvage_outcome(self) -> SimpleNamespace:
        flags: list[str] = []
        if self.graph is not None:
            try:
                flags = list(self.graph.snapshot().flags or [])
            except Exception:
                flags = []
        return SimpleNamespace(
            flag=flags[0] if flags else None,
            flags=flags,
            engine="reason",
        )

    def _absorb_outcome(self, outcome: Any) -> None:
        flags = getattr(outcome, "flags", None) or []
        if getattr(outcome, "flag", None):
            flags = [outcome.flag, *flags]
        if any(flags):
            # Prefer a real resumable CLI outcome over a salvage/summary object.
            current_session = getattr(self._winner_outcome, "session", None)
            new_session = getattr(outcome, "session", None)
            if self._winner_outcome is None or (not current_session and new_session):
                self._winner_outcome = outcome
        for flag in flags:
            if not flag:
                continue
            self.board.write_finding(
                challenge_id=self.challenge.id,
                kind=FindingKind.FLAG_FOUND,
                agent_name=getattr(outcome, "engine", "worker"),
                target=str(flag),
                payload={"flag": str(flag)},
            )
