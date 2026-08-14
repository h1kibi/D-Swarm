"""Reason-centered swarm scheduler.

This scheduler replaces the race path. A run starts with one recon worker,
then Reason audits the board and produces DispatchDecisions. The scheduler
only executes those decisions; it never plans an attack path itself.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.core.provider_errors import (
    ProviderErrorAggregator,
    ProviderErrorDiagnostic,
    classify_provider_error,
)
from dswarm.models.solve_graph import Challenge
from dswarm.swarm.agents import AgentProfile, AgentRegistry, DispatchDecision
from dswarm.swarm.board import (
    Board,
    FindingKind,
    FindingPredicate,
    MemoryBoard,
    PheromoneSettings,
)
from dswarm.solver.worker_profiles import direction_profile_name
from dswarm.solver.reason import ReasonResult

WorkerFactory = Callable[
    [DispatchDecision, AgentProfile],
    Awaitable[Any],
]


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
        self.stop_event = stop_event
        self.graph = graph
        self.projector = projector
        self.max_intents_per_reason = max(1, int(max_intents_per_reason))
        self.reason_debounce = float(reason_debounce)
        self.pause_event = pause_event
        self._last_reason: Optional[ReasonResult] = None
        self._executed: set[str] = set()
        self._fallback_executed = False
        self._planner_failures = 0
        self._last_planner_error: dict[str, Any] = dict(planner_diagnostic or {})
        self._max_planner_failures = int(
            os.environ.get("DSWARM_REASON_MAX_PLANNER_FAILURES", "6") or 6)
        self._generation = 0
        # First flag-bearing worker outcome.  The outer Swarm needs the real CLI
        # session/workdir to persist winner.json; reducing Reason results to strings
        # used to discard that continuation handle.
        self._winner_outcome: Any = None
        self._recovery_attempts: dict[str, int] = {}
        self._max_recovery_attempts = int(
            os.environ.get("DSWARM_WORKER_PROVIDER_RECOVERY_ATTEMPTS", "2") or 2)
        self._paused_profiles: set[str] = set()
        self._provider_errors = ProviderErrorAggregator(
            window_s=float(os.environ.get("DSWARM_PROVIDER_ERROR_WINDOW_S", "60") or 60),
            fatal_threshold=int(os.environ.get("DSWARM_PROVIDER_FATAL_THRESHOLD", "3") or 3),
            majority_ratio=float(os.environ.get("DSWARM_PROVIDER_MAJORITY_RATIO", "0.5") or 0.5),
        )

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
        direction = str(row.get("direction") or "").strip()
        return DispatchDecision(
            intent_id=str(row["intent_id"]),
            goal=str(row.get("goal") or ""),
            profile=direction_profile_name(direction or self.challenge.category)
            or "pi-worker",
            direction=direction,
            mode="explore",
            priority=float(row.get("priority") or 0.5),
            dedupe_key=f"operator:{row.get('directive_id') or row['intent_id']}",
            task_kind=self.challenge.category,
            host_scan=False,
        )

    async def _run_worker(self, decision: DispatchDecision, profile: AgentProfile) -> Any:
        if self.worker_factory is None:
            raise RuntimeError("ReasonSwarm requires a worker_factory")
        return await self.worker_factory(decision, profile)

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

    def _register_decision(self, decision: DispatchDecision) -> None:
        """Persist a Reason dispatch before the worker can produce graph products.

        ReasonSwarm used to emit an ``intent_proposed`` bus delta only.  The worker
        then wrote facts/POCs/conclusions referencing an intent that did not exist in
        SQLite.  Registration is best-effort so an unavailable graph never blocks a
        solve, but when a graph is present the intent row precedes worker execution.
        """
        if self.graph is None:
            return
        worker_class = "review" if decision.mode == "review" else "shell_agent"
        try:
            self.graph.propose_intent(
                actor="reason",
                intent_id=decision.intent_id,
                goal=decision.goal,
                payload={
                    "worker_class": worker_class,
                    "profile": decision.profile,
                    "direction": decision.direction,
                    "mode": decision.mode,
                    "priority": decision.priority,
                    "dedupe_key": decision.dedupe_key,
                    "surface_target": decision.surface_target,
                    "task_kind": decision.task_kind,
                    "host_scan": decision.host_scan,
                },
                from_fact_seqs=decision.from_facts or None,
            )
        except Exception:
            pass

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
        out.update({
            "code": str(diag.get("code") or "planner_exception"),
            "detail": str(diag.get("detail") or str(exc) or type(exc).__name__),
            "planner": str(out.get("planner") or self.reason_model),
        })
        return out

    async def _run_reason(self) -> ReasonResult:
        if self.graph is not None:
            try:
                summary = self.graph.to_reason_summary()
            except Exception:
                summary = self._board_summary()
        else:
            summary = self._board_summary()
        if self.reason_fn is not None:
            result = None
            last_exc: BaseException | None = None
            for attempt in range(3):
                try:
                    result = await self.reason_fn(summary, self.challenge.id)
                    self._last_planner_error = {}
                    break
                except Exception as exc:  # noqa: BLE001 — planner is advisory
                    last_exc = exc
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if result is None:
                if last_exc is not None:
                    self._last_planner_error = self._planner_exception_diag(last_exc)
                result = ReasonResult(
                    goal_met=False,
                    intents=[],
                    audit_notes=["reason planner unavailable"],
                )
        elif self.llm is not None:
            # run-3155: a transient planner outage (provider 5xx/rate-limit) used
            # to collapse the WHOLE run — one failed cycle → fallback → next failed
            # cycle → break → finished. Retry a few times in-cycle with backoff so a
            # short hiccup doesn't terminate the swarm; the caller distinguishes a
            # still-unavailable planner from "planned, produced nothing".
            from dswarm.solver.reason import run_reason

            result = None
            last_exc: BaseException | None = None
            for attempt in range(3):
                try:
                    result = await run_reason(
                        llm=self.llm,
                        model=self.reason_model,
                        graph_summary=summary,
                        run_id=self.challenge.id,
                        challenge_id=self.challenge.id,
                        mode=getattr(self.challenge, "mode", "ctf"),
                        goal=getattr(self.challenge, "goal", "") or None,
                    )
                    self._last_planner_error = {}
                    break
                except Exception as exc:  # noqa: BLE001 — planner is advisory
                    last_exc = exc
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if result is None:
                if last_exc is not None:
                    self._last_planner_error = self._planner_exception_diag(last_exc)
                result = ReasonResult(
                    goal_met=False,
                    intents=[],
                    audit_notes=["reason planner unavailable"],
                )
        else:
            if self._last_planner_error and self._last_planner_error.get("code") != "ok":
                result = ReasonResult(
                    goal_met=False,
                    intents=[],
                    audit_notes=["reason planner unavailable"],
                )
            else:
                result = ReasonResult(goal_met=False, intents=[], audit_notes=[])
        self._last_reason = result
        return result

    def _decisions_from_reason(self, result: ReasonResult) -> list[DispatchDecision]:
        out: list[DispatchDecision] = []
        for it in result.intents:
            mode = it.mode or ("recon" if it.requires_recon else "explore")
            profile_id = (
                direction_profile_name(it.direction)
                or direction_profile_name(self.challenge.category)
                or "pi-worker"
            )
            decision = DispatchDecision(
                intent_id=it.intent_id,
                profile=profile_id,
                direction=it.direction or "",
                goal=it.goal,
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
        recon_profile = self.agents.resolve(
            direction_profile_name(self.challenge.category) or "pi-worker"
        )
        recon_decision = DispatchDecision(
            intent_id="recon-initial",
            profile=recon_profile.id,
            goal=recon_goal,
            mode="recon",
            priority=1.0,
            dedupe_key="recon:recon:initial",
            task_kind=self.challenge.category,
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
            task_kind=recon_decision.task_kind,
        )
        t1 = time.monotonic()
        try:
            recon_outcome = await self._run_worker(recon_decision, recon_profile)
        except Exception:
            recon_outcome = self._salvage_outcome()
        self.board.update_budget(self.challenge.id, delta_hours=(time.monotonic() - t1) / 3600)
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
            )
            tc = time.monotonic()
            result: Optional[ReasonResult] = None
            try:
                result = await self._run_reason()
                if getattr(result, "goal_met", False):
                    break
                decisions = self._decisions_from_reason(result)

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
                    self.board.update_budget(
                        self.challenge.id,
                        delta_hours=elapsed_hours,
                        delta_tokens=0,
                    )
                    self.board.charge_agent(
                        self.challenge.id,
                        profile.id,
                        tokens=0,
                    )
                    self._absorb_outcome(outcome)
                    if self.graph is not None and self.projector is not None:
                        try:
                            self.projector.sync(self.graph)
                        except Exception:
                            pass
                    provider_diag = self._provider_diag_from_outcome(outcome, error)
                    runtime_failed = self._outcome_runtime_failed(
                        outcome, error, provider_diag)
                    fields: dict[str, Any] = {
                        "stage": "execute",
                        "intent_id": decision.intent_id,
                        "profile": profile.id,
                        "mode": decision.mode,
                        "reason_cycle_id": cycle_id,
                        "duration_ms": int((time.monotonic() - t2) * 1000),
                        "flag": getattr(outcome, "flag", None),
                    }
                    if error is not None:
                        fields["error"] = error
                    elif runtime_failed:
                        fields["error"] = (
                            str(provider_diag.get("raw_message") or "")
                            or str(getattr(outcome, "reason", "") or "runtime failure")
                        )
                    if provider_diag:
                        fields["provider_error"] = provider_diag
                        await self._record_provider_error(
                            provider_diag, active_workers=self.max_workers)
                    if runtime_failed and provider_diag.get("retryable"):
                        attempts = self._recovery_attempts.get(decision.dedupe_key, 0) + 1
                        self._recovery_attempts[decision.dedupe_key] = attempts
                        if attempts <= self._max_recovery_attempts:
                            self._executed.discard(decision.dedupe_key)
                            await self._emit(
                                "worker_recovery_scheduled",
                                stage="dispatch",
                                intent_id=decision.intent_id,
                                profile=profile.id,
                                mode=decision.mode,
                                dedupe_key=decision.dedupe_key,
                                reason_cycle_id=cycle_id,
                                recovery_action="redispatch_intent",
                                attempts=attempts,
                                max_attempts=self._max_recovery_attempts,
                                current_worker_interrupted=False,
                                provider_error=provider_diag,
                                operator_message=(
                                    "single-shot worker 当前轮已收尾；retryable provider "
                                    "错误已释放 intent，下一轮 Reason/Worker 会接续消费。"
                                ),
                            )
                        else:
                            await self._emit(
                                "worker_recovery_exhausted",
                                stage="dispatch",
                                intent_id=decision.intent_id,
                                profile=profile.id,
                                dedupe_key=decision.dedupe_key,
                                attempts=attempts,
                                max_attempts=self._max_recovery_attempts,
                                provider_error=provider_diag,
                            )
                    elif runtime_failed and provider_diag.get("should_pause_dispatch"):
                        self._paused_profiles.add(profile.id)
                        await self._emit(
                            "provider_dispatch_paused",
                            stage="dispatch",
                            intent_id=decision.intent_id,
                            profile=profile.id,
                            reason_cycle_id=cycle_id,
                            provider_error=provider_diag,
                            operator_message=(
                                "检测到 fatal provider/account 错误；已暂停该 profile "
                                "后续派发，避免余额/鉴权类错误批量扩散。"
                            ),
                        )
                    await self._emit(
                        "intent_failed" if runtime_failed else "intent_completed",
                        **fields,
                    )

                for decision in decisions:
                    await self._emit(
                        "intent_proposed",
                        stage="reason",
                        intent_id=decision.intent_id,
                        goal=decision.goal,
                        mode=decision.mode,
                        priority=decision.priority,
                        profile=decision.profile,
                        surface_target=decision.surface_target,
                        task_kind=decision.task_kind,
                        host_scan=decision.host_scan,
                        from_facts=decision.from_facts,
                        dedupe_key=decision.dedupe_key,
                        reason_cycle_id=cycle_id,
                    )

                capped = decisions[: self.max_intents_per_reason]
                fresh = [
                    d for d in capped
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
                    if planner_unavailable and not self._flags_complete():
                        self._planner_failures += 1
                        planner_diag = dict(self._last_planner_error or {})
                        planner_diag.setdefault("code", "planner_unavailable")
                        planner_diag.setdefault("detail", "Reason planner unavailable.")
                        planner_diag.setdefault("planner", self.reason_model)
                        await self._emit(
                            "reason_planner_unavailable",
                            stage="reason",
                            reason_cycle_id=cycle_id,
                            failures=self._planner_failures,
                            max_failures=self._max_planner_failures,
                            **planner_diag,
                        )
                        if self._planner_failures >= self._max_planner_failures:
                            await self._emit(
                                "reason_loop_finished",
                                stage="finalize",
                                stop_reason="planner_unavailable",
                                solved=False,
                                generations=self._generation,
                            )
                            break
                        await asyncio.sleep(min(30.0, 5.0 * self._planner_failures))
                        continue
                    self._planner_failures = 0
                    if not self._flags_complete():
                        operator = self._open_operator_decision()
                        if (operator is not None
                                and operator.dedupe_key not in self._executed):
                            # The intent row already exists (proposed by the
                            # coordinator), so no _register_decision here.
                            self._executed.add(operator.dedupe_key)
                            await self._emit(
                                "dispatch_decision",
                                stage="dispatch",
                                intent_id=operator.intent_id,
                                profile=operator.profile,
                                priority=operator.priority,
                                reason_cycle_id=cycle_id,
                                dispatch_reason="operator directive intent",
                            )
                            await _one(operator)
                            continue
                    if not self._flags_complete() and not self._fallback_executed:
                        self._fallback_executed = True
                        fallback = DispatchDecision(
                            intent_id="fallback-bootstrap",
                            profile=(
                                direction_profile_name(self.challenge.category)
                                or "pi-worker"
                            ),
                            goal=(
                                f"Continue solving {self.challenge.name} "
                                f"[{self.challenge.category}]; exploit the confirmed "
                                "surface and recover the flag."
                            ),
                            mode="bootstrap",
                            priority=1.0,
                            dedupe_key="fallback:bootstrap",
                            task_kind=self.challenge.category,
                            host_scan=False,
                        )
                        self._register_decision(fallback)
                        await self._emit(
                            "fallback_dispatch",
                            stage="dispatch",
                            intent_id=fallback.intent_id,
                            reason="no fresh intents",
                            reason_cycle_id=cycle_id,
                        )
                        await _one(fallback)
                        continue
                    break
                for decision in fresh:
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
                    planner=self.reason_model,
                )

        if self.stop_event is not None and self.stop_event.is_set():
            stop_reason = "operator_stop"
        elif self._flags_complete() or getattr(self._last_reason, "goal_met", False):
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
