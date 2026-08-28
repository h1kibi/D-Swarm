"""ReasonSwarm run orchestration.

The run owns the lifecycle around the Reason phase: prepare the shared evidence
graph, schedule typed intents, execute workers, review/revalidate their output,
and finalize only after the provenance gate and completion rules have settled.
The scheduler is the sole live dispatch path; retired race/coordinator mode
configuration is not part of this module's runtime contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from dswarm.learning.distill import TemplateStore

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.runtime_env import is_web_container
from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.core.llm import LLMClient
from dswarm.core.usage_journal import UsageWriter
from dswarm.core.usage_ledger import SpawnGuard
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.solver.runtime_policy import RuntimePolicy, RuntimePolicyError, RuntimeSnapshot
from dswarm.solver.types import SolverConfig, SolveOutcome
from dswarm.solver.credential_accounts import runtime_env_for_engine
from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root, resolve_llm_provider
from dswarm.solver.worker_profiles import (
    base_engine_for_profile,
    coerce_nonneg_int,
    direction_profile_name,
    normalize_profile_roster,
    normalize_worker_profiles,
    profile_names,
)
from dswarm.solver.workspace import cleanup_worker_scratch, ensure_workspace
from dswarm.swarm.cleanup_registry import validate_cleanup_action
from dswarm.swarm.insight_bus import InsightBus
from dswarm.swarm.lane_gate import WorkerLaneGate
from dswarm.swarm.shared_graph import (
    SharedGraph, SQLiteSharedGraph, canonicalize_lane, _is_runtime_infra_fact_text,
)
from dswarm.swarm.poc_verification import sanitize_public_text
from dswarm.swarm.agents import AgentRegistry
from dswarm.swarm.board import Board, MemoryBoard
from dswarm.swarm.blackboard_bridge import BlackboardBridgeMixin
from dswarm.swarm.budget import BudgetMixin, WorkerBudgetExhausted
from dswarm.swarm.errors import WorkerSpawnRejected
from dswarm.swarm.health import (
    HealthMixin,
    _health_cache_clear,
    _health_cache_get,
    _health_cache_put,
)
from dswarm.swarm.projection import BoardProjector
from dswarm.swarm.route_telemetry import MetricsSink
from dswarm.swarm.priority import normalize_priority, normalize_priority_scale
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.swarm.review_capacity import ReviewCapacityMixin
from dswarm.swarm.runtime import SwarmWorkerRuntime
from dswarm.swarm.runtime_degradation import RuntimeDegradationMixin
from dswarm.swarm.review_flow import ReviewFlowMixin
from dswarm.swarm.worker_runtime_mixin import WorkerRuntimeMixin

_WORKER_BACKEND_UNSET = object()
_RUNTIME_POLICY_UNSET = object()

# P0 defect-4: max operator standing hints kept (LRU). The cumulative text is
# injected into EVERY new worker's prompt, so an unbounded list bloated it to the
# point claude empty-exited (~36k tokens). 8 recent hints is plenty of context.


_STANDING_MAX = 8
# M6: cap the outstanding operator-help asks. Deduped on (worker, need) at the sink,
# so this only bites when many DISTINCT blockers pile up on a long never-give-up run;
# bounding it keeps the awaiting_operator count honest and memory flat.
_PENDING_HELP_MAX = 16

_HEALTH_PROBE_CACHE: dict[tuple, "tuple[float, bool, str]"] = {}
# failures expire faster than successes: a transiently-unhealthy engine (cold
# binary, jittery websocket) should get re-probed soon, while a healthy verdict can
# coast the full TTL.










@dataclass
class SwarmOutcome:
    solved: bool
    flag: Optional[str]
    winner: Optional[str]  # solver_id that found the flag
    per_solver: dict[str, SolveOutcome] = field(default_factory=dict)
    reason: str = ""
    # multi-flag: every distinct flag the run collected (flag stays the first).
    flags: list[str] = field(default_factory=list)


class Swarm(
    HealthMixin,
    BlackboardBridgeMixin,
    RuntimeDegradationMixin,
    BudgetMixin,
    ReviewCapacityMixin,
    WorkerRuntimeMixin,
    ReviewFlowMixin,
):
    """Owns one ReasonSwarm run against a challenge."""

    def __init__(
        self,
        challenge: Challenge,
        *,
        llm: LLMClient,
        sandbox: SandboxManager,
        bus: Optional[EventBus] = None,
        cost: Optional[CostController] = None,
        artifacts: Optional[ArtifactStore] = None,
        config: Optional[SolverConfig] = None,
        run_id: Optional[str] = None,
        knowledge: Optional["TemplateStore"] = None,
        hitl_inbox: "Optional[asyncio.Queue]" = None,
        # operator worker commands (spawn/kill a specific engine on demand). The
        # coordinator loop drains this each tick. None → no runtime worker control.
        worker_cmds: "Optional[asyncio.Queue]" = None,
        executor: str = "cli",
        cli_engine: str = "pi",
        # the engine roster this swarm may use, filtered by healthcheck. Defaults
        # to the single pi engine; the web driver passes the configured roster.
        engines: "Optional[list[str]]" = None,
        web_access: bool = True,
        kb: bool = True,
        agent_registry: "Optional[AgentRegistry]" = None,
        board: "Optional[Board]" = None,
        graph_dir: "Optional[Path]" = None,
        worker_root: "Optional[Path]" = None,
        max_workers: int = 10,
        reason_model: str = "deepseek-v4-pro",
        # how many NEW explore workers the coordinator may spawn per loop iteration.
        # 1 = smooth ramp (a slot refills within one ~2s poll anyway); higher values
        # re-introduce the "spawn a burst that shares a fate" problem (run-7352).
        explore_spawn_batch: int = 1,
        # per-turn timeout (s) for an EXPLORE worker's turn-1. Short, because an
        # explore is a narrow single-intent probe — this is the ONLY backstop that
        # frees a slot held by a stuck explore (replacing the old stall-kill).
        # bootstrap/retry keep the long default (whole-challenge rush).
        explore_timeout: int = 720,
        # no-progress backpressure, ALL modes: after this many CONSECUTIVE worker
        # completions with NO new fact (incl. candidates) and NO new flag, the
        # coordinator soft-PAUSES for the operator instead of burning tokens
        # forever. Formerly collect_barren_limit, collect-mode-only and counted
        # idle re-bootstrap rounds — which left single-flag and known-count
        # chained runs (run-11189: expected_flags=15) with NO spend cap, and
        # lived in the fully-idle branch so an intent-churn spike (run-11190:
        # 238 workers) never even reached it. Counting fruitless WORKERS at reap
        # time catches both shapes. Soft pause: no worker kill, any operator
        # input resumes. Generous default — late-stage exploit grinding can
        # legitimately go several workers without a new fact. 0 disables.
        barren_limit: int = 8,
        # NO time limit by default: a CTF challenge has a guaranteed unique
        # solution, so the swarm must NEVER give up on its own — it keeps spawning
        # fresh attempts until it solves or the operator stops it. A clean/offline
        # eval can still cap this by passing a finite budget.
        wall_clock_budget: float = float("inf"),
        # ── worker execution backend ─────────────────────────────────────────
        # "local"  → workers shell out on the HOST (default; unchanged).
        # "container" → workers run inside the run's isolated Docker execution
        #   node, which mounts ONLY the run workspace and account-scoped credential
        #   material. The image is tool-only; credentials are injected at runtime.
        worker_backend: str | object = _WORKER_BACKEND_UNSET,
        runtime_policy: RuntimePolicy | None | object = _RUNTIME_POLICY_UNSET,
        runtime_snapshot: RuntimeSnapshot | None = None,
        pool_manager: Any | None = None,
        runtime_profiles: "Optional[list[dict]]" = None,
        worker_profiles: "Optional[list[dict]]" = None,
        credential_accounts_root: "Optional[Path]" = None,
        blackboard_token: "Optional[str]" = None,
        review_policy: "Optional[dict[str, Any]]" = None,
        max_total_workers: "Optional[int]" = None,
        cost_budget_usd: "Optional[float]" = None,
        llm_profiles: "Optional[dict[str, Any]]" = None,
        llm_providers: "Optional[list[dict[str, Any]]]" = None,
        reason_planner_diagnostic: "Optional[dict[str, Any]]" = None,
        usage_writer: Optional[UsageWriter] = None,
        fallback_usage_writer: Optional[UsageWriter] = None,
        spawn_guard: Optional[SpawnGuard] = None,
        budget_gate: Optional[Any] = None,
        metrics_sink: Optional[Any] = None,
        initial_runtime_operation_kind: str = "",
        cleanup_executor: Optional[Any] = None,
    ) -> None:
        self.challenge = challenge
        self.cleanup_executor = cleanup_executor
        self.llm = llm
        self.usage_writer = usage_writer
        self.fallback_usage_writer = fallback_usage_writer
        self.spawn_guard = spawn_guard
        self.budget_gate = budget_gate
        self.initial_runtime_operation_kind = str(initial_runtime_operation_kind or "")
        self.sandbox = sandbox
        self.bus = bus
        self.cost = cost
        self.artifacts = artifacts
        self.config = config
        self.run_id = run_id or challenge.id
        backend_was_supplied = worker_backend is not _WORKER_BACKEND_UNSET
        policy_was_supplied = runtime_policy is not _RUNTIME_POLICY_UNSET
        selected_backend = "local" if not backend_was_supplied else str(worker_backend)
        selected_policy = None if not policy_was_supplied else runtime_policy
        if selected_policy is not None and not isinstance(selected_policy, RuntimePolicy):
            raise RuntimePolicyError("invalid_runtime_policy")
        if selected_policy is None:
            # Transitional compatibility is intentionally limited to callers that
            # have not entered the M9 runtime-policy API yet.  Explicitly supplying
            # ``runtime_policy=None`` cannot authorize host-local workers.
            if policy_was_supplied and selected_backend == "local":
                raise RuntimePolicyError("local_worker_policy_denied")
            if runtime_snapshot is not None or pool_manager is not None:
                raise RuntimePolicyError("runtime_policy_required")
        elif selected_policy.mode == "docker":
            if backend_was_supplied and selected_backend == "local":
                raise RuntimePolicyError("local_worker_policy_denied")
            if runtime_snapshot is None:
                raise RuntimePolicyError("runtime_snapshot_required")
            if runtime_snapshot.run_id != self.run_id:
                raise RuntimePolicyError("runtime_snapshot_run_mismatch")
            if runtime_snapshot.runtime_policy != selected_policy:
                raise RuntimePolicyError("runtime_policy_snapshot_mismatch")
            if pool_manager is None:
                raise RuntimePolicyError("runtime_manager_required")
            if getattr(pool_manager, "run_id", None) != self.run_id:
                raise RuntimePolicyError("runtime_manager_run_mismatch")
            if getattr(pool_manager, "snapshot", None) is not runtime_snapshot:
                raise RuntimePolicyError("runtime_manager_snapshot_mismatch")
            selected_backend = "container"
        else:
            if not selected_policy.local_workers_allowed:
                raise RuntimePolicyError("local_worker_policy_denied")
            if runtime_snapshot is not None or pool_manager is not None:
                raise RuntimePolicyError("local_runtime_context_must_be_empty")
            if backend_was_supplied and selected_backend != "local":
                raise RuntimePolicyError("local_worker_policy_denied")
            selected_backend = "local"

        self.runtime_policy = selected_policy
        self.runtime_snapshot = runtime_snapshot
        self.pool_manager = pool_manager
        self._runtime_profile_to_pool = MappingProxyType(
            {pool.profile_id: pool.pool_id for pool in runtime_snapshot.pools}
            if runtime_snapshot is not None else {}
        )
        self.worker_backend = selected_backend
        # executor: vestigial knob (CLI is the only path now — shelled claude/codex
        # agentic workers). Kept for call-site compatibility; always builds
        # CliSolvers. The moat is the provenance gate + shared_graph + reason.
        self.executor = executor
        self.cli_engine = cli_engine
        self.llm_profiles = dict(llm_profiles or {})
        self.llm_providers = list(llm_providers or [])
        self.reason_planner_diagnostic = dict(reason_planner_diagnostic or {})
        self.review_policy = self._clean_review_policy(review_policy)
        self._last_review_seq = 0
        self._last_review_proposal_seq = 0
        self._last_directive_seq = 0
        # E: last resource-lock event seq surfaced as a board delta (workers acquire
        # locks directly via the blackboard skill; the coordinator mirrors them to UI).
        self._last_resource_seq = 0
        self._active_review_tasks: set[asyncio.Task] = set()
        self._review_workers_spawned = 0
        self._queued_review_requests: list[dict[str, str]] = []
        self._pending_uncertainty_reviews: list[dict[str, Any]] = []
        self._completed_workers_since_review = 0
        self._last_candidate_review_count = 0
        self._last_unverified_flag_review_seq = 0
        # 0 (or unset) means UNLIMITED, matching max_total_workers / cost_budget_usd
        # below and the drivers.py convention (0 ⇄ inf). A bare `is not None` here
        # used to turn a 0 budget into a literal 0s deadline → instant
        # budget_exhausted. Only a POSITIVE value caps the wall clock.
        if self.llm_profiles.get("planner", {}).get("model"):
            reason_model = str(self.llm_profiles["planner"]["model"])
        # short-task model for hand-raise translation (and any future cheap zh helper):
        # the configured titler, else the planner, else the summarizer's flash default.
        self.titler_model = (
            str(self.llm_profiles.get("titler", {}).get("model") or "")
            or str(self.llm_profiles.get("planner", {}).get("model") or "")
            or "deepseek-v4-flash")
        self.max_total_workers = (
            int(max_total_workers) if max_total_workers not in (None, 0) else
            None
        )
        self.cost_budget_usd = (
            float(cost_budget_usd) if cost_budget_usd not in (None, 0) else
            None
        )
        self._spawned_total = 0
        self._budget_exhausted_kind: str | None = None
        self.worker_profiles = self._clean_worker_profiles(worker_profiles)
        self.runtime_profiles = self._clean_runtime_profiles(runtime_profiles)
        # engine roster (deduped) — now profile names. Legacy values like "claude"
        # expand to every enabled claude profile.
        if self.worker_profiles:
            roster = normalize_profile_roster(engines, self.worker_profiles) if engines else []
            self.engines = roster or profile_names(self.worker_profiles)
            # Order the roster by each profile's (priority, name). The dispatcher
            # (_pick_engine → _healthy_role_candidates) walks self.engines in order
            # and prefers the first not-currently-running candidate, so roster
            # ORDER == dispatch preference. Without this sort the priority field is
            # dead on the dispatch path (the roster kept its assembly order, which
            # for an explicit profile-name list is just declaration order). Sorting
            # here makes priority authoritative for scheduler dispatch and matches
            # what the drag-drop composer writes
            # (top card = lowest priority number = picked first). Stable + total:
            # unknown names (defensive) sink to the end deterministically by name.
            # coerce_nonneg_int (NOT `priority or 100`): priority 0 is a legal,
            # MEANINGFUL value (highest precedence) reachable via hand-edited JSON /
            # API import — `0 or 100` would silently demote it to the default and
            # sink the top-priority profile. coerce also guards a non-int string.
            _prio = {str(p["name"]): (coerce_nonneg_int(p.get("priority"), 100), str(p["name"]))
                     for p in self.worker_profiles}
            self.engines.sort(key=lambda e: _prio.get(e, (10**9, e)))
        else:
            roster = engines if engines else ["pi"]
            seen: set[str] = set()
            self.engines = [e for e in roster if not (e in seen or seen.add(e))]
        self._profiles_by_name: dict[str, dict] = {}
        for p in self.worker_profiles:
            for key in (p.get("name"), p.get("id"), p.get("label")):
                if key:
                    self._profiles_by_name[str(key)] = p
        self._profiles_by_engine: dict[str, list[dict]] = {}
        for p in self.worker_profiles:
            self._profiles_by_engine.setdefault(p["engine"], []).append(p)
        for profiles in self._profiles_by_engine.values():
            profiles.sort(key=lambda p: (coerce_nonneg_int(p.get("priority"), 100), p["id"]))
        self._profile_rr: dict[str, int] = {}
        self._active_profile_by_solver: dict[str, str] = {}
        self._active_profile_role_by_solver: dict[str, str] = {}
        self._active_profile_counts: dict[str, int] = {}
        self._active_review_profile_counts: dict[str, int] = {}
        self._active_account_by_solver: dict[str, str] = {}
        # web_access=False → workers run offline (no WebSearch/WebFetch) for a
        # clean bench eval. kb → let workers use the optional KB MCP
        # (DSWARM_KB_MCP_NAME), if one is configured.
        self.web_access = web_access
        self.kb = kb
        self.agent_registry = agent_registry or AgentRegistry()
        self.board = board
        # worker_root: a persistent per-run dir under which each CLI worker gets
        # its OWN cwd (worker_root/{solver_id}-{n}/) instead of a system $TMPDIR
        # mkdtemp. The web driver points this at sessions/{id}/workspace/workers/
        # so a run's worker scratch (staged attachments, agent-extracted files,
        # PoCs) lives under the run's folder — inspectable after the run and
        # cleaned up with it. None → fall back to mkdtemp (TUI / tests).
        self.worker_root = Path(worker_root) if worker_root is not None else None
        self.workspace_root = self.worker_root.parent if self.worker_root is not None else None
        if self.workspace_root is not None:
            ensure_workspace(self.workspace_root, runtime={
                "backend": self.worker_backend,
                "run_id": self.run_id,
            })
        self.credential_accounts_root = (
            Path(credential_accounts_root).expanduser().resolve()
            if credential_accounts_root is not None else None
        )
        # ModelGateway issues independent per-worker task tokens at spawn time;
        # only the blackboard token remains run-scoped here.
        self._blackboard_token = blackboard_token or os.environ.get("DSWARM_BLACKBOARD_TOKEN", "").strip() or None
        self._runtime_degraded: list[dict[str, Any]] = []
        # engines dropped from the roster by a dispatch-time health-check failure
        # (e.g. cursor headless auth lapsed). engine -> reason. Used to dedup the
        # engine_degraded event (emit once per transition, not once per spawn).
        self._degraded_engines: dict[str, str] = {}
        # health-probe cache: each `_healthy_engines` call shells a REAL one-turn CLI
        # hello per engine (subprocess.run, up to a 60–150s timeout), which is what
        # made dispatch "freeze for ~a minute" before any worker spawned. Cache the
        # (ok, detail) verdict per probe-identity (engine + role + resolved account)
        # for a short TTL so back-to-back dispatches / re-bootstraps don't re-probe a
        # roster we just verified. Keyed on the SHARED process-wide cache below so
        # sibling runs in the same server reuse it too. monotonic clock only.
        self._health_probe_ttl = float(
            os.environ.get("DSWARM_HEALTH_PROBE_TTL", "120") or 120)
        self._worker_seq = 0  # monotonic suffix so two workers never share a cwd
        # per-engine monotonic label counter → unique solver_id per spawn so the
        # deck draws one lane per worker (1st keeps the bare "cli-<engine>" id).
        self._label_seq: dict[str, int] = {}
        self.max_workers = max_workers
        review_limit_raw = self.review_policy.get("max_concurrent", 1)
        try:
            review_limit = max(0, int(review_limit_raw))
        except (TypeError, ValueError):
            review_limit = 1
        self._worker_lane_gate = WorkerLaneGate(
            max_workers=max(0, int(max_workers)),
            review_max_concurrent=review_limit,
        )
        self.reason_model = reason_model
        self.explore_spawn_batch = max(1, int(explore_spawn_batch))
        self.explore_timeout = int(explore_timeout)
        self.barren_limit = int(barren_limit)
        self.wall_clock_budget = wall_clock_budget
        self.knowledge = knowledge  # §16: recall prior + distill on solve
        # Persistent operator "standing" guidance (VPS/SSH creds, global constraints).
        # The coordinator holds the canonical list so EVERY worker — including ones
        # spawned AFTER the operator gave the hint — gets it injected into its turn-1
        # prompt. Before this, standing only reached a worker via its live InsightBus
        # inbox, which lands AFTER turn-1's prompt is already built (and many explore
        # workers finish in one turn), so late-spawned workers never saw the VPS hint.
        self._standing_guidance: "list[str]" = []
        # ── intent-level HITL (single-shot migration, M-3) ────────────────────
        # Workers are single-shot now (DESIGN_single_shot_migration.md): they don't
        # resume to absorb operator guidance mid-run. So a non-standing hint/redirect
        # can no longer steer a LIVE worker — it must reach the NEXT spawned one.
        # _target_redirect holds an operator-supplied new target URL (applied to every
        # subsequent worker); _next_worker_guidance holds one-shot hint/redirect text
        # consumed by the next _make_cli_worker spawn, then cleared. This is the
        # accepted granularity degrade: turn-level live steering → intent-level.
        self._target_redirect: "Optional[str]" = None
        self._next_worker_guidance: "list[str]" = []
        # ── operator-blocked state (worker raised its hand / env down) ────────
        # When a worker emits a HITL_REQUEST (NEED_INPUT / env_down), the coordinator
        # stops re-spawning that dead-end direction and WAITS for the operator instead
        # of burning tokens retrying a blocker no agent can clear (no VPS, expired
        # target). _pending_help holds the outstanding asks; _operator_event is set by
        # _drain_hitl on ANY operator command, which unblocks the wait.
        self._pending_help: "list[dict]" = []
        # M11: idempotency guard so the coordinator's run finalization (persist winner +
        # close shared_graph + RUN_FINISHED + worker-dir cleanup) runs EXACTLY once,
        # whether the loop returns normally OR is cancelled/errors out through the finally.
        self._run_finalized = False
        # winner.json is continuation state, not a solve signal.  A failed best-
        # effort write must remain observable without spamming one delta per
        # finalize/retry path (and without exposing the payload or filesystem
        # path in the public event stream).
        self._winner_persist_failure_noted = False
        self._reason_db_failures_noted: set[str] = set()
        # L3: bus sinks the coordinator added (help / submit-gate), detached on finalize
        # so a reused bus (standby/resolve restart re-entering the coordinator) doesn't
        # accumulate Swarm-closing sinks across cycles.
        self._coord_sinks: "list" = []
        self._operator_event: "Optional[asyncio.Event]" = None
        # operator STOP: a `stop`/`complete` HITL command ends the coordinator loop
        # gracefully (distinct from a steer, which only guides workers). Needed for
        # challenges that never yield a gated flag — without it the "never give up"
        # re-bootstrap runs forever (run-10070: 74 workers on an already-solved box).
        self._operator_stop: bool = False
        # operator PAUSE (#5): a `pause` HITL command SOFT-pauses the coordinator —
        # it stops spawning NEW workers and waits, but does NOT terminate the run
        # (distinct from stop). `resume` clears it. This is the meaningful "pause" for
        # a single-shot architecture (freezing one about-to-exit worker is near
        # worthless); the operator's intent is "stop burning budget on new workers
        # while I look / wait". The wait reuses _operator_event (set by any command).
        self._operator_paused: bool = False
        self.insight = InsightBus(challenge.id)
        # HITL: a queue the frontend posts human commands onto (hint/redirect/
        # pause/resume, scoped global or per-solver). A background task drains it
        # into insight.guidance() so the broadcast reaches every solver's inbox.
        self.hitl_inbox = hitl_inbox
        self.worker_cmds = worker_cmds
        # P-A: ONE shared, event-sourced, evidence-bearing graph for the swarm.
        # Route telemetry is an independent best-effort sidecar: failure to create
        # its directory must never disable or prevent opening the canonical graph.
        self._graph_dir = Path(graph_dir) if graph_dir is not None else None
        self._route_metrics = metrics_sink
        if self._route_metrics is None:
            try:
                metrics_root = (
                    self._graph_dir.parent
                    if self._graph_dir is not None
                    else self.sandbox.root / self.run_id
                )
                self._route_metrics = MetricsSink(metrics_root, run_id=self.run_id)
            except Exception:
                self._route_metrics = None

        self.shared_graph: Optional[SharedGraph] = None
        try:
            # graph_dir (web driver) keeps the DB OUTSIDE sandbox.root so it
            # survives sandbox.shutdown_all()'s rmtree of the sandbox root. Falls
            # back to the sandbox tree when unset (TUI / tests, where ephemeral
            # is fine).
            if self._graph_dir is not None:
                self._graph_dir.mkdir(parents=True, exist_ok=True)
                db_path = self._graph_dir / "shared_graph.db"
            else:
                db_path = self.sandbox.root / self.run_id / "shared_graph.db"
            self.shared_graph = SQLiteSharedGraph.open(
                db_path=db_path, challenge=challenge, artifacts=artifacts,
                metrics_sink=self._route_metrics,
            )
        except Exception:
            # The shared graph is additive; never block solving if it cannot open.
            self.shared_graph = None
        self._last_graph_event_seq = 0
        self._graph_bridge_failures: dict[int, int] = {}
        # multi-flag: the authoritative dedup set of flags collected so far. The
        # run is "solved" once it holds expected_flags distinct flags. For a
        # single-flag challenge (expected_flags=1) the first flag fills it and
        # _flags_complete() flips true immediately — byte-identical to the old
        # "first flag wins" behaviour.
        self._found_flags: list[str] = []


    def pool_id_for_profile(self, profile_id: str) -> str:
        """Return the frozen run-scoped pool identity for one Worker profile."""
        try:
            return self._runtime_profile_to_pool[str(profile_id)]
        except KeyError as exc:
            raise RuntimePolicyError("runtime_profile_not_in_snapshot") from exc

    def _expected_flags(self) -> int:
        return max(1, getattr(self.challenge, "expected_flags", 1) or 1)

    def _multi_flag(self) -> bool:
        return bool(getattr(self.challenge, "multi_flag", False))

    def _flags_complete(self) -> bool:
        """Is the run's flag objective met? This is the SAVE-vs-FINISH decoupling
        (run-10070): saving a flag (_record_flags) must not finish a collect-mode run
        the way it finishes a single-flag run.

        Every decision point first reconciles against the authoritative shared-graph
        snapshot (_sync_flags_from_graph, the run-75379 fix): a flag that reached
        the graph through a path without a clean reaped outcome still counts toward
        completion. If the graph is unavailable the memory set alone decides —
        exactly the pre-sync behavior.

        - single-flag (multi_flag=False, the default): `len >= expected_flags`, which
          with expected_flags=1 finishes on the first gated flag — byte-identical to
          the old behavior.
        - collect mode with a known count (multi_flag=True, expected_flags>1): finish
          once N distinct flags are collected.
        - collect mode with UNKNOWN count (multi_flag=True, expected_flags<=1): NEVER
          finish by count. Flags still save + display; the run ends only on operator
          STOP or the coordinator's no-progress pause. A saved flag is not a finish."""
        self._sync_flags_from_graph()
        if self._multi_flag() and self._expected_flags() <= 1:
            return False
        return len(self._found_flags) >= self._expected_flags()

    def _record_flags(self, *flags: Optional[str]) -> list[str]:
        """Add flags to the dedup set; return the ones that were NEW (so the caller
        can broadcast each exactly once)."""
        fresh: list[str] = []
        for f in flags:
            if f and f not in self._found_flags:
                self._found_flags.append(f)
                fresh.append(f)
        return fresh

    def _sync_flags_from_graph(self) -> list[str]:
        """Reconcile the in-memory flag set with the AUTHORITATIVE shared-graph
        snapshot, returning the flags that were newly absorbed (for one-time
        broadcast). This is the fix for the run-75379 split-brain (BUG②/④).

        Every worker writes each accepted flag to the shared graph via
        cli_solver._accept_flag → shared_graph.flag_found, and the graph snapshot
        is what the UI / planner / finalize already trust. But _found_flags (the
        in-memory list this class reads for completion) was fed ONLY from reaped
        `outcome.flags`, so a flag that reached the graph via a path that never
        delivered a clean outcome — a worker cancelled after it accepted a flag,
        an error-reaped worker, or the live-broadcast/DB-bridge path — stayed
        invisible to the completion check. In run-75379 the graph held 4 valid
        flags (5 found, 1 operator-invalidated) while _found_flags was stuck at 2,
        so _flags_complete() never fired and the run spawned ~55 post-solve waves
        until operator stop.

        Reconciling against snapshot().flags makes the graph the single source of
        truth for completion; _flags_complete() calls this before every verdict
        so no completion decision site can forget to sync:
          - ADD any flag the graph holds but _found_flags is missing.
          - DROP any flag the operator explicitly INVALIDATED (snapshot already
            excludes it), so a blacklisted false positive (e.g. 090099b7) can never
            count toward expected_flags (BUG③ cross-check).
        Absent-from-snapshot-but-not-invalidated flags are LEFT in place: a silent
        flag_found DB-write failure must not let a genuinely-held flag vanish from
        the count."""
        if self.shared_graph is None:
            return []
        try:
            graph_flags = list(getattr(self.shared_graph.snapshot(), "flags", []) or [])
            invalidated = self.shared_graph.invalidated_flags()
        except Exception:
            return []
        # DROP operator-invalidated flags from the in-memory set (and never let one
        # back in below). reopen_after_false_positive removes it from the snapshot
        # too, so this only matters for a flag already absorbed before invalidation.
        if invalidated:
            self._found_flags = [f for f in self._found_flags if f not in invalidated]
        # ADD any authoritative flag the in-memory set is missing.
        fresh = self._record_flags(*(f for f in graph_flags if f not in invalidated))
        return fresh


    async def _reconcile_blackboard_skill(self) -> None:
        """Once per run, BEFORE any worker launches: make sure the deployed user-scope
        blackboard skill copies match the repo source, re-syncing stale/missing ones.

        Source runs invoke the repo skill directly, but a worker CLI can still
        auto-discover a ROTTED user-scope copy for unprompted skill use — run-75378
        shipped workers a skill missing the whole G0-G4 + lifecycle landing because the
        deployed copy was never re-synced after the repo skill advanced. This closes
        that gap loudly: anything actually re-synced is printed AND emitted as a board
        delta so a silent drift can never recur unnoticed. Containerized workers use the
        image-baked skill, so skip them. Best-effort — never blocks the run."""
        if self.worker_backend == "container":
            return
        try:
            from dswarm.solver.blackboard_skill import sync_deployed_blackboard_skills
            rows = await asyncio.to_thread(sync_deployed_blackboard_skills)
        except Exception:
            return
        synced = [r for r in rows if r.get("status") == "synced"]
        errored = [r for r in rows if r.get("status") == "error"]
        if synced:
            for r in synced:
                print(f"[blackboard-skill] re-synced stale deployed copy "
                      f"{r['path']} (was {r.get('was')} → {r.get('now')})")
            await self._emit_bb_bus(
                "skill_resynced",
                summary=(f"deployed dswarm-blackboard skill was stale at "
                         f"{len(synced)} location(s); re-synced from repo source"),
                paths=[r["path"] for r in synced])
        for r in errored:
            print(f"[blackboard-skill] WARNING: could not sync {r['path']}: "
                  f"{r.get('error')}")

    async def _emit_bb_bus(self, kind: str, **fields) -> None:
        """Emit one BLACKBOARD_DELTA from anywhere (finalize, resolve, etc.) — the
        coordinator loop has its own `_emit_bb` closure, but lifecycle transitions at
        run finish happen outside it and must still reach the JSONL/SSE stream the UI
        reads. Best-effort; a bus failure never masks the outcome."""
        if self.bus is None:
            return
        try:
            await self.bus.emit(Event(
                event_type=EventType.BLACKBOARD_DELTA, run_id=self.run_id,
                challenge_id=self.challenge.id,
                payload=blackboard_delta_payload(kind, actor="coordinator", **fields)))
        except Exception:
            pass

    async def _emit_finalize_lifecycle_deltas(self, result: dict, reason: str) -> None:
        """Emit only lifecycle deltas not already present in shared_graph events.

        release_claims_for_finalize() appends intent_state_changed rows for
        resume/operator-stop transitions, so _drain_graph_to_bus() can replay those
        in event-sequence order.  Solved-run closure currently records only an
        intent_concluded row, so we still synthesize the closed state for the deck.
        """
        if not isinstance(result, dict):
            return
        if (reason or "").strip() != "solved":
            return
        closed = [str(x) for x in (result.get("closed_intents") or []) if x]
        if closed:
            await self._emit_bb_bus(
                "intent_state_changed", intent_id=",".join(closed),
                dispatch_state="closed", close_reason="closed_by_solve",
                stop_reason="solved")

    _GRAPH_BRIDGE_KINDS = {
        "fact_added",
        "dead_end",
        "intent_proposed",
        "intent_claimed",
        "intent_concluded",
        "intent_state_changed",
        "flag_found",
        "flag_unverified",
        "poc_saved",
        "poc_claimed",
        "poc_concluded",
        "poc_reproduction_registered",
        "poc_reproduction_rejected",
        "poc_verification_started",
        "poc_verified",
        "poc_verification_failed",
        "review_finding",
        "review_finding_verified",
        "route_suppressed",
        "route_reopened",
        "branch_split",
        "branch_resolved",
        "coordinator_directive",
        "cleanup_action_registered",
        "cleanup_executed",
        "cleanup_failed",
    }

    async def _drain_graph_to_bus(self, *, emit_bb) -> None:
        if self.shared_graph is None:
            return
        try:
            events = self.shared_graph.events_since(
                self._last_graph_event_seq,
                kinds=sorted(self._GRAPH_BRIDGE_KINDS),
            )
        except Exception:
            return
        for ev in events:
            seq = int(ev.get("seq") or 0)
            emissions = self._graph_event_to_bb(ev)
            try:
                for kind, fields in emissions:
                    await emit_bb(kind, **fields)
            except Exception:
                fails = self._graph_bridge_failures.get(seq, 0) + 1
                self._graph_bridge_failures[seq] = fails
                if fails >= 3:
                    self._last_graph_event_seq = max(self._last_graph_event_seq, seq)
                    self._graph_bridge_failures.pop(seq, None)
                    continue
                return
            self._last_graph_event_seq = max(self._last_graph_event_seq, seq)
            self._graph_bridge_failures.pop(seq, None)

    async def _emit_run_finished(self, *, flag: "Optional[str]", solved: bool,
                                 reason: str = "finished") -> None:
        """Emit the ONE run-level RUN_FINISHED for this swarm run. Sub-workers emit
        WORKER_FINISHED (worker-level), so this is the single signal that flips the
        deck/rail to 'finished'. Best-effort: a bus failure must not mask the
        outcome the caller is about to return.

        Payload carries `flag` (first, back-compat), `flags` (all collected), and
        `expected_flags` so the deck can render N/total + decide solved-vs-partial."""
        self._cleanup_finished_worker_dirs()
        if self.bus is None:
            return
        try:
            runtime_meta = self._runtime_metadata_for()
            await self.bus.emit(Event(
                event_type=EventType.RUN_FINISHED, run_id=self.run_id,
                challenge_id=self.challenge.id,
                payload={"flag": flag, "flags": list(self._found_flags),
                         "expected_flags": self._expected_flags(),
                         "multi_flag": self._multi_flag(),
                         "solved": solved,
                         "reason": reason,
                         **runtime_meta}))
        except Exception:
            pass

    def _refresh_workspace_board(self) -> None:
        """Materialize the final graph state into the run-root board atomically."""
        if self.shared_graph is None or self.workspace_root is None:
            return
        board_path = self.workspace_root / ".dswarm_board.md"
        marker = "<!-- dswarm-team-board -->"
        rendered = str(self.shared_graph.to_board_markdown() or "").lstrip()
        content = rendered if rendered.startswith(marker) else f"{marker}\n{rendered}"
        if not content.endswith("\n"):
            content += "\n"
        board_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = board_path.with_name(
            f".{board_path.name}.tmp.{os.getpid()}.{id(self)}")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(tmp_path, board_path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    async def _execute_registered_cleanups(self) -> None:
        """Execute registered typed actions in reverse registration order.

        This method intentionally has no shell fallback.  ``remove_artifact`` is
        handled as a bounded, run-workspace filesystem operation; listener/session/
        credential actions require an injected runtime adapter that already owns the
        frozen run generation.  Each failure becomes an append-only audit event and
        never prevents claims release or graph closure.
        """
        graph = self.shared_graph
        if graph is None:
            return
        getter = getattr(graph, "cleanup_actions", None)
        if not callable(getter):
            return
        try:
            actions = list(getter(include_terminal=False) or [])
        except Exception:
            return
        for action in reversed(actions):
            action_id = str(action.get("action_id") or "")
            if not action_id:
                continue
            try:
                result = await self._execute_one_cleanup_action(action)
                graph.cleanup_action_executed(
                    actor="coordinator", action_id=action_id, result=str(result or ""),
                )
            except Exception as exc:
                try:
                    graph.cleanup_action_failed(
                        actor="coordinator", action_id=action_id,
                        # Keep private adapter/path details out of the durable
                        # failure record as well as the public bridge.
                        reason=sanitize_public_text(type(exc).__name__, limit=160),
                    )
                except Exception:
                    # The action itself is already isolated from the finalize path;
                    # do not let a secondary audit write failure strand the run.
                    pass

    async def _execute_one_cleanup_action(self, action: dict[str, Any]) -> Any:
        action_type = str(action.get("action_type") or "")
        target = str(action.get("target") or "")
        try:
            validate_cleanup_action(
                action_type, target, actor=action.get("actor") or "coordinator",
                owner_key=action.get("owner_key") or action.get("actor") or "coordinator",
            )
        except ValueError as exc:
            raise RuntimeError(f"invalid registered cleanup action: {exc}") from exc
        if action_type == "remove_artifact":
            root = self.workspace_root
            if root is None and self.shared_graph is not None:
                try:
                    root = Path(str(getattr(self.shared_graph, "db_path"))).resolve().parent.parent
                except Exception:
                    root = None
            if root is None:
                raise RuntimeError("cleanup workspace unavailable")
            root_path = Path(root).resolve()
            # Keep the final path lexical so a symlink target is unlinked rather
            # than followed. Validate the parent after resolving it to prevent a
            # symlinked directory below workers/ from reaching outside the run.
            candidate = root_path / Path(target)
            workers = (root_path / "workers").resolve()
            try:
                candidate.relative_to(workers)
                candidate.parent.resolve().relative_to(workers)
            except ValueError as exc:
                raise RuntimeError("artifact cleanup target outside workers") from exc
            if candidate.is_dir() and not candidate.is_symlink():
                raise RuntimeError("artifact cleanup target is a directory")
            # Missing is success: cleanup actions are idempotent.
            existed = candidate.exists() or candidate.is_symlink()
            if existed:
                candidate.unlink()
            return "removed" if existed else "already absent"
        executor = self.cleanup_executor
        if not callable(executor):
            raise RuntimeError(f"typed cleanup executor unavailable for {action_type}")
        result = executor(dict(action))
        if hasattr(result, "__await__"):
            result = await result
        if result is False:
            raise RuntimeError(f"typed cleanup executor rejected {action_type}")
        return result if result is not True else "completed"

    async def _finalize_coordinator_run(
        self, *, winner: "Optional[str]", flag: "Optional[str]",
        goal_complete: bool, per_solver: "dict[str, SolveOutcome]",
        terminal_reason: str = "",
        winner_outcome: "Optional[SolveOutcome]" = None) -> None:
        """M11: persist the winner, close the shared graph (release the SQLite WAL/-shm
        handles), and emit the single run-level RUN_FINISHED (which also sweeps
        non-winner worker scratch dirs). Idempotent via _run_finalized — safe to call
        from BOTH the normal-return path and the coordinator's finally, so a cancelled
        / errored run still frees its DB handle and cleans scratch instead of leaking
        them (the cleanup used to sit AFTER the finally, on the normal path only)."""
        if self._run_finalized:
            return
        self._run_finalized = True
        # L3: detach the coordinator's bus sinks so a reused bus doesn't keep them.
        if self.bus is not None and self._coord_sinks:
            for sink in self._coord_sinks:
                try:
                    self.bus.remove_sink(sink)
                except Exception:
                    pass
            self._coord_sinks = []
        if winner is not None:
            self._persist_winner(winner_outcome or per_solver.get(winner), flag)
        solved = winner is not None or goal_complete or self._flags_complete()
        reason = (terminal_reason or "").strip()
        if not reason:
            if solved:
                reason = "solved" if winner is not None or self._flags_complete() else "goal_met"
            elif self._operator_stop:
                reason = "operator_stop"
            elif self._budget_exhausted_kind:
                reason = "budget_exhausted"
            else:
                reason = "runtime_failure"
        if self.shared_graph is not None:
            # Pentest scope review runs after all workers have written their
            # effective evidence and before graph events are bridged/closed.
            # It is intentionally a no-op for CTF runs or missing scope.
            try:
                self._run_scope_audit()
            except Exception:
                # Scope review is an audit side effect; it must never hide the
                # run outcome or prevent the append-only graph from finalizing.
                pass
            try:
                snap = self.shared_graph.snapshot()
                self._record_flags(*getattr(snap, "flags", []))
            except Exception:
                pass
            finalize_reason = (
                reason if reason in {"solved", "operator_stop", "budget_exhausted", "runtime_failure"}
                else ("solved" if solved else "runtime_failure"))
            fin: dict = {}
            try:
                # M9 typed cleanup runs before claims are released, while the run
                # ownership context is still available. It is best-effort and has
                # no raw-command or host-shell fallback.
                await self._execute_registered_cleanups()
            except Exception:
                pass
            try:
                await self._drain_graph_to_bus(emit_bb=self._emit_bb_bus)
            except Exception:
                pass
            try:
                fin = self.shared_graph.release_claims_for_finalize(  # type: ignore[attr-defined]
                    reason=finalize_reason)
            except Exception:
                pass
            try:
                await self._drain_graph_to_bus(emit_bb=self._emit_bb_bus)
            except Exception:
                pass
            if fin:
                try:
                    # Mirror solved-run closure that is not represented by a graph
                    # intent_state_changed row. Resume/operator-stop state changes
                    # are replayed by the drain above in original event order.
                    await self._emit_finalize_lifecycle_deltas(fin, finalize_reason)
                except Exception:
                    pass
            # The root board is a materialized view of the graph. Refresh it after
            # final lifecycle transitions even when bus emission/draining failed.
            try:
                self._refresh_workspace_board()
            except Exception:
                pass
            try:
                self.shared_graph.close()
            except Exception:
                pass
        solved = winner is not None or goal_complete or self._flags_complete()
        if solved and (not terminal_reason or reason == "runtime_failure"):
            reason = "solved" if winner is not None or self._flags_complete() else "goal_met"
        finish_flag = self._found_flags[0] if self._found_flags else (
            flag if winner is not None else None)
        await self._emit_run_finished(flag=finish_flag, solved=solved,
                                      reason=reason)

    def _cleanup_finished_worker_dirs(self) -> None:
        """Remove failed/finished worker scratch while preserving durable run data.

        The workspace root keeps shared/, inputs/, graph/, final/, manifest.json,
        and winner.json.  Only non-winner worker cwd directories under workers/ are
        removed at run finish to avoid long coordinator runs accumulating hundreds
        of duplicate scratch trees.
        """
        if self.worker_root is None:
            return
        keep: list[str] = []
        if self.workspace_root is not None:
            winner = self.workspace_root / "winner.json"
            try:
                data = json.loads(winner.read_text(encoding="utf-8"))
                workdir = data.get("workdir")
                if workdir:
                    keep.append(Path(str(workdir)).name)
            except Exception:
                pass
        cleanup_worker_scratch(self.worker_root, keep=keep)

    async def _drain_hitl(self) -> None:
        """Background: pull human commands off hitl_inbox and broadcast them to
        every solver via the InsightBus. Runs until cancelled. Each item is a
        dict {target, action, text} (the shape RunManager.post_hitl enqueues)."""
        if self.hitl_inbox is None:
            return
        while True:
            cmd = await self.hitl_inbox.get()
            try:
                if not isinstance(cmd, dict):
                    continue
                text = cmd.get("text") or cmd.get("hint") or ""
                action = cmd.get("action") or "hint"
                target = cmd.get("target") or "global"
                # operator STOP/COMPLETE: end the run gracefully. Unlike a steer
                # (which only guides workers), this terminates the coordinator loop —
                # the lever for a challenge that never yields a gated flag. Wake the
                # coordinator so it checks the flag at its next iteration boundary.
                if action in ("stop", "complete"):
                    self._operator_stop = True
                    self._pending_help = []
                    if self._operator_event is not None:
                        self._operator_event.set()
                    if getattr(self, "_reason_stop_event", None) is not None:
                        self._reason_stop_event.set()
                    # unblock a paused ReasonSwarm loop so it can observe the
                    # stop_event at the top of its next iteration
                    reason_gate = getattr(self, "_reason_pause_gate", None)
                    if reason_gate is not None:
                        reason_gate.set()
                    await self.insight.guidance(
                        text, action="stop", target=target, standing=False)
                    continue
                # operator PAUSE/RESUME (#5): soft-pause the coordinator's spawn loop.
                # pause sets a flag the loop checks at its top (no new workers until
                # resume); it does NOT kill running workers or end the run. resume
                # clears it and wakes the loop. This is the contract that actually fits
                # a single-shot swarm — see _operator_paused. Still broadcast on the
                # InsightBus below (the deck reflects pause/resume; a live standby
                # worker also gets it). We don't `continue` for resume so it falls
                # through to the wake (set _operator_event) at the bottom.
                if action == "pause":
                    self._operator_paused = True
                    # gate the ReasonSwarm loop too — it only checks its
                    # pause_event, not _operator_paused
                    reason_gate = getattr(self, "_reason_pause_gate", None)
                    if reason_gate is not None:
                        reason_gate.clear()
                    # surface it on the board so the rail shows "paused"
                    await self._emit_coord_bb(
                        "operator_paused",
                        reason="operator paused the swarm "
                               "(no new workers until resume)")
                    await self.insight.guidance(
                        text, action="pause", target=target, standing=False)
                    continue
                if action == "resume":
                    self._operator_paused = False
                    reason_gate = getattr(self, "_reason_pause_gate", None)
                    if reason_gate is not None:
                        reason_gate.set()
                    if self._operator_event is not None:
                        self._operator_event.set()
                    await self.insight.guidance(
                        text, action="resume", target=target, standing=False)
                    continue
                # DISMISS a worker's hand-raise (NEED_INPUT) WITHOUT supplying the
                # resource: the operator judges the ask a false alarm / not worth
                # answering. The swarm must NOT stay frozen waiting on a blocker the
                # operator won't clear. Clear the pending ask (scoped to target),
                # record a dead-end so a re-spawned worker doesn't immediately re-raise
                # the same thing, unfreeze the workers, and wake the coordinator. No
                # resource is injected (distinct from a hint/redirect that answers it).
                if action in ("dismiss", "dismiss_help"):
                    if target == "global":
                        dismissed = list(self._pending_help)
                        self._pending_help = []
                    else:
                        scoped = target.split(":", 1)[-1] if ":" in target else target
                        dismissed = [h for h in self._pending_help
                                     if str(h.get("worker", "")) == scoped]
                        self._pending_help = [h for h in self._pending_help
                                              if str(h.get("worker", "")) != scoped]
                    for h in dismissed:
                        need = str(h.get("need", "")).strip()
                        if need:
                            try:
                                await self.insight.dead_end(
                                    "coordinator",
                                    f"operator dismissed the ask «{need[:160]}» — "
                                    f"not supplying it; do not re-raise")
                            except Exception:
                                pass
                    self._operator_paused = False
                    reason_gate = getattr(self, "_reason_pause_gate", None)
                    if reason_gate is not None:
                        reason_gate.set()
                    if self._operator_event is not None:
                        self._operator_event.set()
                    # SIGCONT the workers we froze on the hand-raise so the swarm
                    # resumes instead of sitting paused on a dismissed blocker.
                    try:
                        await self.insight.guidance(
                            "", action="resume", target="global", standing=False)
                    except Exception:
                        pass
                    await self._emit_coord_bb(
                        "help_dismissed",
                        reason=f"operator dismissed {len(dismissed)} hand-raise(s)"
                               f"{'' if target == 'global' else ' for ' + target}",
                        count=len(dismissed))
                    continue
                # P0 defect-4: clear standing guidance. The list is only-grew before,
                # so an operator who dropped several corrections could not retract a
                # stale one (and the cumulative text bloated every new worker's prompt
                # → claude 36k-token empty-exit). clear_standing wipes all, or one by
                # exact text match (cmd["text"]).
                if action in ("clear_standing", "reset_guidance"):
                    if text:
                        self._standing_guidance = [
                            s for s in self._standing_guidance if s != text]
                    else:
                        self._standing_guidance = []
                    continue
                if action == "mark_false":
                    flag = str(cmd.get("flag") or "").strip()
                    if not flag and text:
                        m = re.search(r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}", str(text))
                        flag = m.group(0) if m else str(text).strip()
                    if not flag and self._found_flags:
                        flag = self._found_flags[0]
                    if flag:
                        self._found_flags = [f for f in self._found_flags if f != flag]
                        info = {"dead_end_reason": f"false positive: {flag}",
                                "reopened": []}
                        if self.shared_graph is not None:
                            try:
                                info = self.shared_graph.reopen_after_false_positive(
                                    actor="operator", flag=flag)
                            except Exception:
                                pass
                        await self._emit_coord_bb(
                            "dead_end", reason=info.get("dead_end_reason")
                            or f"false positive: {flag}")
                        for iid in info.get("reopened", []) or []:
                            await self._emit_coord_bb("intent_reopened", intent_id=iid)
                        await self._emit_coord_bb("flag_invalidated", flag=flag)
                        if self.bus is not None:
                            try:
                                await self.bus.emit(Event(
                                    event_type=EventType.RUN_REOPENED,
                                    run_id=self.run_id,
                                    challenge_id=self.challenge.id,
                                    payload={"flag": flag},
                                ))
                            except Exception:
                                pass
                        if self._operator_event is not None:
                            self._operator_event.set()
                    continue
                # `url` is the NEW target a redirect carries (distinct from `target`,
                # which is the SCOPE: global / solver:<id>). `standing` marks
                # persistent background guidance (VPS/SSH creds) for all workers.
                url = cmd.get("url") or cmd.get("target_url") or ""
                standing = bool(cmd.get("standing", False))
                # persist standing guidance on the coordinator so workers spawned
                # LATER inherit it at turn-1 (live workers also get it via the
                # InsightBus broadcast below). Dedupe so re-sends don't pile up.
                if standing and text and text not in self._standing_guidance:
                    self._standing_guidance.append(text)
                    # P0 defect-4: LRU cap — keep only the most recent N standing
                    # hints so the cumulative text can't bloat every new worker's
                    # prompt unbounded (the 36k-token claude empty-exit). The per-
                    # worker char budget (cli_solver _standing_block) is the second
                    # guard; this bounds the count at the source.
                    if len(self._standing_guidance) > _STANDING_MAX:
                        self._standing_guidance = self._standing_guidance[-_STANDING_MAX:]
                # M-3 (single-shot migration): a NON-standing hint/redirect can no
                # longer steer a live (single-shot) worker — route it to the NEXT
                # spawned worker. A redirect url becomes the new target for every
                # subsequent worker; hint/redirect text is one-shot guidance the next
                # spawn folds in. (standing already flows via _standing_guidance.)
                if not standing:
                    if url:
                        self._target_redirect = url
                    if text and text not in self._next_worker_guidance:
                        self._next_worker_guidance.append(text)
                # B: record the steer as a FIRST-CLASS OperatorDirective (not a fake
                # low-confidence candidate + ordinary intent). The directive carries a
                # preemption policy; soft_rebind (default) supersedes unclaimed
                # conflicting intents so the next worker batch picks up the new
                # direction, without killing a live worker. graceful_drain / force_cancel
                # are honored where the operator explicitly asks for them.
                preempt = str(cmd.get("preempt_policy")
                              or cmd.get("preemption") or "").strip().lower()
                if text and self.shared_graph is not None and action in (
                        "hint", "focus", "redirect", "directive", "correction"):
                    try:
                        info = self.shared_graph.add_operator_directive(
                            actor="operator", action=action, text=text,
                            scope=target or "global", standing=standing,
                            preempt_policy=preempt or "soft_rebind",
                        )
                        directive_id = info["directive_id"]
                        policy = info["preempt_policy"]
                        # bind: open a directive-tagged intent the next batch can claim.
                        self.shared_graph.propose_intent(
                            actor="operator", intent_id=f"I-{directive_id}", goal=text,
                            payload={"source": "operator_directive", "action": action,
                                     "directive_id": directive_id,
                                     # pi-only roster: operator intents must be
                                     # claimable by a shell_agent worker, and routed
                                     # to the challenge's direction profile.
                                     "worker_class": "shell_agent",
                                     "direction": direction_profile_name(
                                         self.challenge.category),
                                     "priority": "operator"},
                        )
                        self.shared_graph.update_directive_status(
                            directive_id=directive_id, status="bound",
                            generated_intent_id=f"I-{directive_id}")
                        await self._emit_coord_bb(
                            "operator_directive_changed", directive_id=directive_id,
                            action=action, text=text, status="bound",
                            preemption=policy, intent_id=f"I-{directive_id}")
                        # soft_rebind / graceful_drain / force_cancel: retire UNCLAIMED
                        # conflicting "ask operator" directions (the redirect obsoletes
                        # them). Live workers are only touched on graceful_drain (a
                        # GUIDANCE drain signal) / force_cancel (handled via _drain below).
                        if policy in ("soft_rebind", "graceful_drain", "force_cancel"):
                            for needle in ("operator", "ask", "request"):
                                try:
                                    self.shared_graph.supersede_open_intents(
                                        actor="coordinator", match=needle,
                                        reason=f"superseded by operator directive {directive_id}")
                                except Exception:
                                    pass
                        if policy == "graceful_drain":
                            try:
                                await self.insight.guidance(
                                    text, action="graceful_drain", target=target,
                                    standing=False)
                            except Exception:
                                pass
                        if self.review_policy.get("on_operator_hint", True):
                            self._queue_review_request(
                                trigger="operator_hint",
                                directive=(
                                    f"Operator {action} directive was added: {text}. "
                                    "Audit whether this should become a route change, "
                                    "branch split, fact challenge, or focused worker directive."
                                ),
                            )
                    except Exception:
                        pass
                # still broadcast on the InsightBus: the deck's event log + a live
                # standby worker consume it. A racing single-shot worker ignores it
                # (it has no resume turn) — that's the accepted intent-level degrade.
                await self.insight.guidance(
                    text, action=action, target=target, url=url, standing=standing)
                # wake the coordinator if it had paused (the operator supplied input).
                if self._operator_event is not None:
                    self._operator_event.set()
                # M5: clear the "waiting for help" asks SCOPED to the command's target.
                # A global command answers every pending ask; a solver-scoped one
                # (target == "solver:<id>") only clears that worker's ask, so a hint
                # addressed to worker B no longer wipes worker A's still-unmet blocker
                # (which would resolve awaiting_operator with no real answer and resume
                # hurling workers at A's wall). Keep the rest pending.
                if target == "global":
                    self._pending_help = []
                else:
                    scoped = target.split(":", 1)[-1] if ":" in target else target
                    self._pending_help = [
                        h for h in self._pending_help
                        if str(h.get("worker", "")) != scoped]
                # M3: RETIRE the now-obsolete "ask the operator for X" intents ONLY when
                # the operator actually SUPPLIED A RESOURCE — a redirect url, standing
                # guidance, or hint text (run-11190: 238-worker loop re-asking for the
                # L2 SSH password after it was supplied). A bare default-action hint with
                # no content used to run this sweep too, and its broad substring needles
                # (operator/unlock/dashboard) could wrongly retire a legitimate in-flight
                # intent on a totally unrelated hint. Gate on a resource being present;
                # for a solver-scoped command, only retire that worker's blocked intents.
                gave_resource = bool(url) or standing or bool(text)
                if self.shared_graph is not None and gave_resource:
                    superseded = 0
                    for needle in ("operator", "ssh password", "dashboard",
                                   "unlock"):
                        try:
                            superseded += len(self.shared_graph.supersede_open_intents(
                                actor="coordinator", match=needle,
                                reason=f"operator supplied input ({action})"))
                        except Exception:
                            pass
                    if superseded:
                        try:
                            await self.insight.dead_end(
                                "coordinator",
                                f"retired {superseded} obsolete 'ask-operator' "
                                f"intent(s) after operator input")
                        except Exception:
                            pass
            except Exception:
                # a malformed command must never kill the drain loop
                continue

    async def run(self) -> SwarmOutcome:
        # Per-run profile/runtime pools outlive an individual solve pass. Their
        # deletion/archive/shutdown lifecycle is owned by the run manager, not by
        # ordinary completion of Swarm.run().
        await self._reconcile_blackboard_skill()
        try:
            if self.executor == "cli":
                return await self._run_reason_scheduler()
            raise RuntimeError(f"unsupported executor: {self.executor}")
        finally:
            # Defense in depth: worker finally paths revoke individual tokens;
            # run completion removes any token left by a crash/late construction.
            try:
                from dswarm.solver.modelgateway import ModelGateway
                ModelGateway.instance().revoke_run(self.run_id)
            except Exception:
                pass

    @staticmethod
    def _cancel_solver(solver: Any) -> None:
        """Stop a solver's underlying work (kills a CLI worker's subprocess). A
        plain task.cancel() only unschedules the asyncio task — the shelled CLI
        agent kept running. Solvers that don't expose cancel() (code-driven) are a
        no-op here; the task cancel still stops them between turns."""
        if solver is None:
            return
        fn = getattr(solver, "cancel", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    async def _run_reason_scheduler(self) -> SwarmOutcome:
        """Run the Reason-centered swarm: initial recon, then Reason dispatch."""
        board = self.board
        if board is None:
            dsn = os.environ.get("DSWARM_BOARD_DSN", "").strip()
            if dsn:
                from dswarm.swarm.postgres_board import PostgresBoard

                board = PostgresBoard(dsn, challenge_id=self.challenge.id)
            else:
                board = MemoryBoard(self.challenge.id)
        await self._emit_coord_bb(
            "worker_health_check",
            phase="starting",
            role="bootstrap",
            engines=list(self.engines),
        )
        try:
            healthy = await self._healthy_engines_async()
        except Exception as exc:  # noqa: BLE001
            await self._emit_coord_bb(
                "worker_health_check",
                phase="failed",
                role="bootstrap",
                engines=list(self.engines),
                healthy=[],
                reason=str(exc)[:240],
            )
            await self._finalize_coordinator_run(
                winner=None,
                flag=None,
                goal_complete=False,
                per_solver={},
                terminal_reason="worker_unavailable",
            )
            return SwarmOutcome(
                False, None, None, {}, "worker_unavailable", flags=[]
            )
        await self._emit_coord_bb(
            "worker_health_check",
            phase="completed" if healthy else "failed",
            role="bootstrap",
            engines=list(self.engines),
            healthy=list(healthy),
            reason="" if healthy else "no healthy worker profile",
        )
        if not healthy:
            await self._finalize_coordinator_run(
                winner=None,
                flag=None,
                goal_complete=False,
                per_solver={},
                terminal_reason="worker_unavailable",
            )
            return SwarmOutcome(
                False, None, None, {}, "worker_unavailable", flags=[]
            )
        stop_event: asyncio.Event = asyncio.Event()
        self._reason_stop_event = stop_event
        # pause gate for the ReasonSwarm loop: set = running, cleared = paused.
        # ReasonSwarm awaits this event at the top of each reason cycle
        # (reason_scheduler.py), so pause/resume must clear/set it here — the
        # coordinator path's _operator_paused flag alone never reaches it.
        pause_gate: asyncio.Event = asyncio.Event()
        pause_gate.set()
        self._reason_pause_gate = pause_gate
        hitl_task = None
        if self.hitl_inbox is not None:
            hitl_task = asyncio.create_task(self._drain_hitl(), name="reason-hitl-drain")
        projector = BoardProjector(
            board, after_seq=0, bus=self.bus, run_id=self.run_id,
            challenge_id=self.challenge.id, metrics_sink=self._route_metrics,
        )
        if self.shared_graph is not None:
            try:
                projector.sync(self.shared_graph)
            except Exception:
                pass

        runtime = SwarmWorkerRuntime(self, healthy, projector=projector)

        swarm = ReasonSwarm(
            self.challenge,
            board=board,
            agents=self.agent_registry,
            llm=self.llm,
            reason_model=self.reason_model,
            bus=self.bus,
            run_id=self.run_id,
            max_workers=self.max_workers,
            wall_clock_budget=self.wall_clock_budget,
            worker_factory=runtime.run,
            stop_event=stop_event,
            graph=self.shared_graph,
            projector=projector,
            pause_event=pause_gate,
            planner_diagnostic=self.reason_planner_diagnostic,
            lane_gate=self._worker_lane_gate,
            initial_runtime_operation_kind=self.initial_runtime_operation_kind,
        )
        try:
            out = await swarm.run()
            flags = list(out.get("flags") or [])
            solved = bool(out.get("solved") or flags)
            winner_outcome = out.get("winner_outcome")
            winner_name = (
                (getattr(winner_outcome, "engine", "") or "reason")
                if solved else None
            )
            reason = (
                "runtime_unavailable"
                if runtime.runtime_unavailable
                else "operator_stop"
                if stop_event.is_set()
                else "solved via reason swarm" if solved else "reason swarm stopped"
            )
            self._record_flags(*flags)
            await self._finalize_coordinator_run(
                winner=winner_name,
                flag=flags[0] if flags else None,
                goal_complete=False,
                per_solver={},
                terminal_reason=reason,
                winner_outcome=winner_outcome,
            )
            return SwarmOutcome(
                solved=solved,
                flag=flags[0] if flags else None,
                winner=winner_name,
                reason=reason,
                flags=flags,
            )
        finally:
            if hitl_task is not None:
                hitl_task.cancel()
                await asyncio.gather(hitl_task, return_exceptions=True)
            # Normal completion finalized above.  On cancellation/error this closes
            # the graph, releases claims, refreshes the materialized board, and emits
            # the single terminal event.
            try:
                await self._finalize_coordinator_run(
                    winner=None, flag=None, goal_complete=False, per_solver={},
                    terminal_reason=(
                        "runtime_unavailable"
                        if runtime.runtime_unavailable
                        else "operator_stop" if stop_event.is_set() else "runtime_failure"
                    ),
                )
            except Exception:
                pass








































    def _retry_goal(self) -> str:
        """Course-correction goal for a re-bootstrap.

        A retry_bootstrap worker runs the SAME _run_bootstrap path as the initial
        rush — same 80 turns, same prompt — so it CAN go just as deep. The only
        difference is this goal text, injected as a "Course correction" block. The
        old wording ("re-examine assumptions / try a different angle / from scratch")
        made the agent treat the run as exploratory reconsideration: it did a few
        probes, saw the board already covered them, and concluded "nothing new" in
        seconds (run-7349: retry workers did 0-5 tool calls vs 24-32 for bootstrap).

        So we push the OPPOSITE: the board's verified facts are a HEAD-START to build
        on, not re-derive; pick the most promising half-finished attack chain and
        DRIVE IT TO A WORKING EXPLOIT / the flag, exactly like a first-time solve.
        Dead-ends are listed only as "already ruled out — don't waste time there"."""
        deadends: list[str] = []
        sg = getattr(self, "shared_graph", None)
        if sg is not None:
            try:
                for e in sg.events():
                    if e.get("kind") == "dead_end":
                        # the reason lives in the event's JSON payload, not at the
                        # top level — reading e.get("reason") always returned "" so
                        # the dead-end list was silently empty before this fix.
                        p = e.get("payload") or {}
                        r = (p.get("reason") or p.get("text")
                             or e.get("reason") or "").strip()
                        if r:
                            deadends.append(r[:160])
            except Exception:
                deadends = []
        head = (
            "This challenge HAS a solution and is NOT yet solved. The shared board "
            "above already has verified facts — treat them as a HEAD-START, not work "
            "to redo. Pick the most promising lead or half-finished attack chain and "
            "DRIVE IT ALL THE WAY to a working exploit and the flag — run real "
            "commands, chain the steps, do not stop at recon. Go as deep as a "
            "first-time solve (you have the full turn budget). Only treat the run as "
            "done when you have the flag from real output or have genuinely exhausted "
            "this lead. If a lead is truly dead, switch to a different bug class / "
            "endpoint and push that to completion too — do not conclude after a few "
            "probes.")
        if deadends:
            body = "\n".join(f"  - {d}" for d in deadends[-12:])
            return (f"{head}\n\nAlready ruled out (do NOT retry these — pick "
                    f"something else):\n{body}")
        return head

    def _open_intents(self) -> list[dict]:
        """Intents available to (re)dispatch: never-claimed (status='open') PLUS any
        claimed intent whose LEASE EXPIRED (its worker died/stalled and never
        concluded). Closing this lease loop is what lets the swarm recover an intent
        abandoned by a stuck worker — without it, a worker that hangs holding a claim
        would orphan that intent forever (claim_intent already honors expired leases,
        but the coordinator never re-read them, so they were lost)."""
        if self.shared_graph is None:
            return []
        import time as _time
        now = _time.time()
        try:
            rows = self.shared_graph.dispatchable_intents(now=now)
            out: list[dict] = []
            inferred_lanes: list[tuple[str, str, str]] = []
            seen_routes: set[str] = set()
            for r in rows:
                wc = str(r.get("worker_class") or "code")
                direction = str(r.get("direction") or "")
                route = str(r.get("route_hash") or "")
                if (route and wc not in {"verifier", "review"}
                        and hasattr(self.shared_graph, "is_route_suppressed")
                        and self.shared_graph.is_route_suppressed(route)):
                    continue
                if route and wc not in {"verifier", "review"}:
                    if route in seen_routes:
                        continue
                    seen_routes.add(route)
                lane_key = str(r.get("lane_key") or "")
                risk_class = str(r.get("risk_class") or "")
                resource_key = str(r.get("resource_key") or "")
                # E: dispatch preflight — skip an intent whose declared resource is
                # currently locked by ANOTHER worker (route around it, don't collide).
                if (resource_key and hasattr(self.shared_graph, "check_resource_conflicts")):
                    try:
                        conflict = self.shared_graph.check_resource_conflicts(
                            resource_key=resource_key)
                        if conflict.get("conflict"):
                            continue
                    except Exception:
                        pass
                if not lane_key:
                    hint = self._lane_hint_from_text(
                        str(r.get("goal") or ""), require_control_hint=True)
                    lane_key = str(hint.get("lane_key") or "")
                    if lane_key:
                        risk_class = str(hint.get("risk_class") or risk_class or "")
                        inferred_lanes.append((lane_key, risk_class,
                                               str(r.get("intent_id") or "")))
                out.append({
                    "intent_id": r.get("intent_id"),
                    "goal": r.get("goal"),
                    "worker_class": wc,
                    "route_hash": route,
                    "branch_id": str(r.get("branch_id") or ""),
                    "priority": normalize_priority(r.get("priority")),
                    "priority_scale": normalize_priority_scale(
                        r.get("priority_scale"), raw_priority=r.get("priority")
                    ),
                    "lane_key": lane_key,
                    "risk_class": risk_class,
                    "resource_key": resource_key,
                    "direction": direction,
                })
            if inferred_lanes:
                for lane_key, risk_class, intent_id in inferred_lanes:
                    if not intent_id:
                        continue
                    self.shared_graph.annotate_intent_lane(
                        intent_id=intent_id,
                        lane_key=lane_key,
                        risk_class=risk_class or lane_key.split(":", 1)[0],
                    )
            return out
        except Exception:
            return []

    def _ordinary_open_queue_depth(self, open_intents: Optional[list[dict]] = None) -> int:
        intents = self._open_intents() if open_intents is None else open_intents
        return sum(
            1 for it in intents
            if str(it.get("worker_class") or "code") in {"code", "shell_agent"}
        )

    def _pick_open_intent_for_spawn(self, engine: str) -> "Optional[dict]":
        """First open, dispatchable, ordinary intent this spawn can serve — or None.

        D (run-3154 intent starvation): generic bootstrap/retry/rebootstrap spawns
        used to always run a whole-challenge rush, so a focused reason intent could
        sit open/unclaimed while generic workers churned through capacity (I3 open
        30+ min). When such a spawn is about to happen, prefer handing it the oldest
        open intent it is compatible with. Compatibility: the intent's `direction`
        must map to a worker profile that matches the engine being spawned (an
        intent with no direction is fine for any ordinary worker); verifier/review
        intents keep their own lanes. The atomic claim happens in
        `_make_cli_worker` under the worker's solver_id.
        """
        if self.shared_graph is None:
            return None
        try:
            for it in self._open_intents():
                wc = str(it.get("worker_class") or "code")
                if wc not in {"code", "shell_agent"}:
                    continue
                direction = str(it.get("direction") or "")
                if direction:
                    pid = direction_profile_name(direction)
                    if pid and pid != engine:
                        continue
                return it
        except Exception:
            return None
        return None

    def _reason_backpressure_active(self, open_intents: list[dict]) -> bool:
        return self._ordinary_open_queue_depth(open_intents) >= max(1, 2 * self.max_workers)







    async def _run_reason(self) -> int:
        """Reason phase: pro model reads the board, proposes intents. Returns the
        number of new intents proposed. Advisory — never raises into the loop.

        Side effect: stashes the latest verdict/drift in self._last_reason so the
        coordinator can act on a course_correct (phase 7: adaptive re-bootstrap)."""
        if self.shared_graph is None or self.llm is None:
            return 0
        try:
            from dswarm.solver.reason import run_reason, dispatch_intents
            # P1.5: un-blind the planner. The default max_evidence=16 hard-capped
            # Reason at the last 16 facts (swarm re-planned against a truncated view
            # and kept dispatching re-work — a co-equal root cause of the long-chain
            # re-discovery in run-10067). to_reason_summary renders the FULL board
            # (all facts AND all dead-ends — the old call left dead-ends clipped to
            # the last 8) PLUS the in-flight and attempted-with-results intent
            # sections, so the planner stops re-proposing directions that are
            # already running or already concluded (run-11190 paraphrase churn).
            # [#seq] fact labels survive — they are Reason's `from`-citation
            # mechanism (the {fact_ids} allow-list a plan may cite).
            summary = self.shared_graph.to_reason_summary(
                standing_guidance=list(self._standing_guidance))
            try:
                fact_index = self.shared_graph.fact_pin_context()
            except Exception:
                fact_index = ""
            result = await run_reason(
                llm=self.llm, model=self.reason_model, graph_summary=summary,
                fact_index=fact_index,
                max_intents=4, run_id=self.run_id, challenge_id=self.challenge.id,
                # pentest → judge completion against the operator's engagement goal
                # (CTF passes mode="ctf" + no goal → the prompt is byte-identical).
                mode=getattr(self.challenge, "mode", "ctf"),
                goal=(getattr(self.challenge, "goal", "") or None),
            )
            self._last_reason = result
            try:
                pins = getattr(result, "pinned_facts", []) or []
                if pins:
                    self.shared_graph.pin_facts(
                        actor="reason", fact_seqs=list(pins),
                        reason="reason model selected durable retention facts")
            except Exception as exc:
                await self._note_reason_db_failure(
                    "fact_db_write_failed", "pin_facts", exc)
            try:
                proposed = dispatch_intents(self.shared_graph, result, actor="reason")
            except Exception as exc:
                # The legacy/advisory Reason entry point is still exercised by
                # callers and tests even though the live scheduler owns dispatch.
                # Do not let a failed intent append disappear behind its outer
                # best-effort return value.
                await self._note_reason_db_failure(
                    "intent_db_write_failed", "propose", exc)
                return 0
            for it in proposed:
                if self.bus is not None:
                    await self.bus.emit(Event(
                        event_type=EventType.BLACKBOARD_DELTA,
                        run_id=self.run_id, challenge_id=self.challenge.id,
                        payload=blackboard_delta_payload(
                            "intent_proposed", actor="reason",
                            intent_id=it["intent_id"], goal=it["goal"],
                            worker_class=it["worker_class"],
                            from_facts=it.get("from_facts", [])),
                    ))
                # zh gist for the (often long, English) Reason goal — reuse the
                # planner's own llm client; fire-and-forget so planning isn't held up.
                self._summarize_intent_async(it["intent_id"], it["goal"])
            return len(proposed)
        except Exception:
            return 0

    def _summarize_intent_async(self, intent_id: str, goal: str) -> None:
        """Fire-and-forget a deepseek-flash zh gist for a Reason intent goal."""
        if self.bus is None or len((goal or "").strip()) < 48:
            return
        from dswarm.solver.summarizer import summarize_node
        try:
            asyncio.create_task(summarize_node(
                goal, node_kind="intent", intent_id=intent_id,
                shared_graph=self.shared_graph, llm=self.llm,
                bus=self.bus, run_id=self.run_id, challenge_id=self.challenge.id,
                solver_id="summarizer", usage_writer=self.usage_writer))
        except RuntimeError:
            pass

    async def _emit_coord_bb(self, kind: str, **fields) -> None:
        """Emit a coordinator-scoped blackboard delta for the scheduler loop."""
        if self.bus is None:
            return
        try:
            await self.bus.emit(Event(
                event_type=EventType.BLACKBOARD_DELTA, run_id=self.run_id,
                challenge_id=self.challenge.id,
                payload=blackboard_delta_payload(kind, actor="coordinator", **fields)))
        except Exception:
            pass

    async def _note_reason_db_failure(
            self, kind: str, op: str, exc: BaseException) -> None:
        """Surface one bounded durable-write diagnostic from the advisory path.

        This path must remain best-effort: the event is useful operator telemetry,
        but neither a failed fact pin nor a failed intent append may alter worker
        execution or provenance/flag acceptance.  Publish only the exception class;
        filesystem/database messages can contain private paths or payload details.
        """
        key = f"{kind}:{op}"
        if key in self._reason_db_failures_noted:
            return
        if len(self._reason_db_failures_noted) >= 32:
            self._reason_db_failures_noted.clear()
        self._reason_db_failures_noted.add(key)
        await self._emit_coord_bb(
            kind,
            op=op,
            reason=sanitize_public_text(type(exc).__name__, limit=160),
        )















    def _persist_winner(self, outcome: "Optional[SolveOutcome]", flag: "Optional[str]") -> None:
        """Write the winner's CLI continuation handle to workspace/winner.json so a
        post-solve standby driver can resume the SAME session for a human
        follow-up. Best-effort: a write failure must never fail a solved run.

        Needs graph_dir (web runs) — winner.json lands beside graph/ (a sibling of
        the sandbox root, so sandbox.shutdown_all()'s rmtree can't delete it). TUI
        / test runs without graph_dir simply skip persistence (no standby there)."""
        if self._graph_dir is None or outcome is None:
            return
        session = getattr(outcome, "session", None)
        # only CLI workers carry a session; without one there's nothing to resume.
        if not session:
            return
        try:
            import json
            payload = {
                "engine": getattr(outcome, "engine", "") or "",
                "session": session,
                "workdir": getattr(outcome, "workdir", "") or "",
                "flag": flag or outcome.flag or "",
                # multi-flag: every flag the run collected (the run's authoritative
                # set, not just this one worker's). `flag` stays the first.
                "flags": list(self._found_flags) or (
                    [flag] if flag else (outcome.flags or [])),
                "challenge": self.challenge.model_dump(),
                **self._runtime_metadata_for(outcome),
            }
            dest = self._graph_dir.parent / "winner.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            if self._winner_persist_failure_noted:
                return
            self._winner_persist_failure_noted = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            try:
                loop.create_task(self._emit_coord_bb(
                    "winner_persist_failed",
                    op="winner_json",
                    # Filesystem exceptions often include an absolute host path;
                    # publish only the stable exception class, never that path.
                    reason=sanitize_public_text(type(exc).__name__, limit=160),
                ))
            except Exception:
                # Diagnostics are strictly best-effort; never turn a solved run
                # into a failure merely because its telemetry cannot be queued.
                pass


async def run_swarm(
    challenge: Challenge,
    *,
    llm: LLMClient,
    sandbox: SandboxManager,
    bus: Optional[EventBus] = None,
    cost: Optional[CostController] = None,
    artifacts: Optional[ArtifactStore] = None,
    config: Optional[SolverConfig] = None,
    run_id: Optional[str] = None,
) -> SwarmOutcome:
    """Functional entry point mirroring §5.4's run_swarm signature."""
    return await Swarm(
        challenge, llm=llm, sandbox=sandbox, bus=bus, cost=cost,
        artifacts=artifacts, config=config, run_id=run_id,
    ).run()
