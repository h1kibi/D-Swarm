"""RunManager — the web/TUI-facing handle to live solve runs.

The frontends are dumb subscribers (§3): they never call the solver core
directly. They ask the RunManager to start a run, then subscribe to that run's
EventBus and POST HITL commands which land in the run's HITL queue. This keeps
the event schema as the only contract between core and UI.

A "run" here is one challenge being solved (solo or by a swarm). Each gets its
own EventBus + SessionStore (durable replay) + an asyncio.Queue for inbound
human commands.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from apps.web.provider_errors import ProviderErrorAggregator, classify_provider_error
from apps.web.run_meta import FolderStore, RunMetaStore
from apps.web.worker_config import WorkerConfigStore
from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.events import Event, EventType, hitl_response_payload
from dswarm.core.session_store import SessionStore
from dswarm.core.usage_journal import UsageContext, UsageJournal, UsageRecord, UsageWriter
from dswarm.core.usage_ledger import SpawnGuard, UsageLedger
from dswarm.swarm.budget import ProfileBudgetGate
from dswarm.solver.credential_accounts import ensure_pi_account_from_env
from dswarm.solver.container_pool import ContainerPoolManager
from dswarm.solver.runtime_policy import RuntimePolicy, RuntimePolicyError, RuntimeSnapshot
from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuilder, RuntimeSnapshotStore
from dswarm.solver.runtime_cleanup import RuntimeCleanupInspector, RuntimeCleanupResult

LOG = logging.getLogger(__name__)


def merge_resolve_dispatch(
    saved: dict[str, Any] | None,
    body: dict[str, Any] | None,
    historical_challenge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a continuation request without losing an operator direction.

    Direction uses presence semantics: an omitted field preserves the saved
    operator choice, an explicit empty string resets to ``auto``, and a value
    replaces the saved choice. Other challenge fields retain the historical
    challenge before applying explicit request overrides.
    """
    saved_body = dict(saved or {})
    request_body = dict(body or {})
    saved_ch = saved_body.get("challenge")
    saved_ch = dict(saved_ch) if isinstance(saved_ch, dict) else {}
    historical = dict(historical_challenge or {})
    request_ch = request_body.get("challenge")
    request_ch = dict(request_ch) if isinstance(request_ch, dict) else {}
    challenge = {**saved_ch, **historical, **request_ch}
    # ``direction`` has three-state presence semantics rather than ordinary
    # historical merge semantics: an explicit request wins, otherwise retain
    # the saved operator choice, and only use the historical value when no
    # saved choice exists.  This prevents replay metadata from silently
    # changing the operator's selected route during /resolve.
    if "direction" in request_ch:
        challenge["direction"] = request_ch["direction"]
    elif "direction" in saved_ch:
        challenge["direction"] = saved_ch["direction"]
    elif "direction" in historical:
        challenge["direction"] = historical["direction"]
    else:
        challenge.pop("direction", None)
    merged = {**saved_body, **request_body, "challenge": challenge}
    return merged


@dataclass
class Run:
    run_id: str
    bus: EventBus
    cost: CostController
    store: SessionStore
    usage_journal: UsageJournal | None = None
    ledger: UsageLedger | None = None
    spawn_guard: SpawnGuard | None = None
    budget_gate: ProfileBudgetGate = field(default_factory=ProfileBudgetGate)
    runtime_policy: RuntimePolicy | None = None
    runtime_snapshot: RuntimeSnapshot | None = None
    pool_manager: ContainerPoolManager | None = None
    hitl: "asyncio.Queue[dict[str, Any]]" = field(default_factory=asyncio.Queue)
    # operator worker commands (spawn/kill a specific engine) the coordinator drains
    worker_cmds: "asyncio.Queue[dict[str, Any]]" = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    # post-solve standby: a short-lived worker spun up to serve a HITL command when
    # the main run is no longer live (finished, or the server restarted). Serialized
    # — one at a time per run.
    standby_task: Optional[asyncio.Task] = None
    finished: bool = False
    flag: Optional[str] = None
    # multi-flag: every distinct flag the run collected (dedup, discovery order).
    # `flag` stays the first for back-compat. expected_flags drives the rail/UI
    # "collected N/total" + the solved-vs-collecting distinction.
    flags: list[str] = field(default_factory=list)
    expected_flags: int = 1
    # multi-flag MODE bit (collect vs single). Relayed on the synthetic RUN_FINISHED
    # so a reconnecting deck knows a collect run shouldn't read "solved" on flag #1.
    multi_flag: bool = False
    # ---- lightweight metadata for the thread rail (conversation-first deck) ----
    # The deck lists runs in a ChatGPT-style sidebar; it needs a name/category/
    # outcome per run without replaying the whole event stream. We sniff these off
    # the bus as a sink (the run stays a dumb event source — no extra contract).
    name: str = ""
    category: str = ""
    started: bool = False
    solved: bool = False
    paused: bool = False
    # a worker raised its hand (HITL_REQUEST: NEED_INPUT / target crashed / instance
    # expired / missing credential). True until the operator answers (HITL_RESPONSE)
    # or the run finishes. Surfaced on the summary so a poll of /api/runs catches it
    # — independent of `paused` (the swarm may keep running with one hand up).
    awaiting_help: bool = False
    help_text: str = ""
    created_seq: int = 0
    updated_seq: int = 0  # bumped on every event — exposed as activity metadata
    updated_at: float = 0.0  # epoch seconds of the latest event, for rail "x ago"
    # operator-set rail metadata (persisted in RunMetaStore, injected by manager)
    pinned: bool = False
    pinned_at: Optional[float] = None
    archived: bool = False
    custom_name: Optional[str] = None
    # rail folder (None = top-level) + operator drag-order within its section
    folder_id: Optional[str] = None
    sort_order: Optional[int] = None
    # M2: signature of the last HITL command (target, action, text, url) — an
    # identical back-to-back resend is dropped instead of re-queued/re-emitted.
    _last_hitl_sig: Optional[tuple] = None
    # P4: scheduler state — True while the run waits for a concurrency slot
    # (started but not dispatched); queue_position is the 1-based FIFO position.
    # cancelled marks an operator-cancelled run (queued-cancel, or a live run
    # stopped via the cancel endpoint) — distinct from a plain finished run.
    queued: bool = False
    queue_position: Optional[int] = None
    cancelled: bool = False
    blackboard_token: str = ""
    # Safe, durable copy of the dispatch options used to create this run. A
    # finished run can be continued after a server restart without silently
    # falling back to a different global worker roster/backend. Secrets are
    # redacted before this is stored (see remember_dispatch).
    dispatch_body: dict[str, Any] = field(default_factory=dict)

    def merge_flags(self, flags: Any) -> None:
        """Accumulate flags from an event payload (dedup, keep order); keep the
        flag/flags[0] invariant. Accepts a list or a single string."""
        if isinstance(flags, str):
            flags = [flags]
        for f in (flags or []):
            if f and f not in self.flags:
                self.flags.append(f)
        if self.flags and not self.flag:
            self.flag = self.flags[0]

    def status(self) -> str:
        """Single derived lifecycle status the rail renders an icon for.

        draft → never started. queued → started but waiting for a scheduler
        slot. running → started, not finished, not paused. paused → operator
        paused a live run (or held a queued one). solved/finished/failed are
        terminal; cancelled is a terminal operator abort.
        """
        if self.cancelled:
            return "cancelled"
        if not self.started:
            return "draft"
        if self.queued:
            return "paused" if self.paused else "queued"
        if not self.finished:
            return "paused" if self.paused else "running"
        if self.solved:
            return "solved"
        return "finished"  # ended, no flag (we don't distinguish "failed" yet)

    def summary(self) -> dict[str, Any]:
        """The shape the deck's thread rail consumes (one row per run)."""
        return {
            "run_id": self.run_id,
            # custom_name (operator rename) wins; else the auto/challenge name.
            # Empty when neither is set — the rail renders its own placeholder, we
            # do NOT leak the bare run id as a display name.
            "name": self.custom_name or self.name,
            "category": self.category or "",
            "started": self.started,
            "finished": self.finished,
            "solved": self.solved,
            "paused": self.paused,
            "awaiting_help": self.awaiting_help,
            "help_text": self.help_text,
            "status": self.status(),
            "queued": self.queued,
            "queue_position": self.queue_position,
            "cancelled": self.cancelled,
            "flag": self.flag,
            "flags": list(self.flags),
            "expected_flags": self.expected_flags,
            "multi_flag": self.multi_flag,
            "pinned": self.pinned,
            "pinned_at": self.pinned_at,
            "archived": self.archived,
            "folder_id": self.folder_id,
            # operator drag-order if set, else creation order (rail sorts by this)
            "order": self.sort_order if self.sort_order is not None else self.created_seq,
            "updated": self.updated_seq,
            "updated_at": self.updated_at,
        }


# A driver is any coroutine fn(run) that emits onto run.bus and returns.
Driver = Callable[[Run], Awaitable[None]]


def _apply_blackboard_meta(run: "Run", ev: Event) -> None:
    """Reflect coordinator BLACKBOARD_DELTA lifecycle into the rail/summary state so
    the deck shows mid-run progress, not just the terminal RUN_FINISHED. Two things
    the operator complained were invisible (run-11189):
      • flag_found — a flag landed mid-run (collect mode keeps going); merge it into
        run.flags NOW so the N/total counter ticks up instead of staying 0 until the
        run ends.
      • awaiting_operator / collect_idle — the swarm auto-paused waiting for the
        operator (NEED_INPUT). Flip run.paused so the rail shows "paused", not a
        spinner that looks like it's still churning. operator_resumed / a STOP clears
        it (RUN_FINISHED already clears paused on its own)."""
    if ev.event_type is not EventType.BLACKBOARD_DELTA:
        return
    kind = (ev.payload or {}).get("kind")
    if kind == "flag_found":
        run.merge_flags((ev.payload or {}).get("flag"))
    elif kind in ("awaiting_operator", "collect_idle"):
        run.paused = True
    elif kind in ("operator_resumed", "operator_stopped"):
        run.paused = False


class RunManager:
    def __init__(
        self,
        *,
        sessions_root: "str | Path | None" = None,
        runtime_snapshot_store: RuntimeSnapshotStore | None = None,
        runtime_snapshot_builder: RuntimeSnapshotBuilder | None = None,
        runtime_pool_manager_factory: Callable[..., ContainerPoolManager] | None = None,
        runtime_cleanup_inspector: RuntimeCleanupInspector | Any | None = None,
    ) -> None:
        # P2-v3: in the compose layout the sessions/ tree must live UNDER the
        # mirrored data root (DSWARM_HOST_DATA_ROOT bind-mounted into the web
        # container), so worker sibling containers — launched by the host daemon —
        # can bind-mount the same physical dir. DSWARM_SESSIONS_ROOT names it
        # (compose points it at <data root>/sessions). Default "sessions" (CWD-
        # relative) preserves the bare-host behaviour.
        if sessions_root is None:
            sessions_root = os.environ.get("DSWARM_SESSIONS_ROOT") or "sessions"
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.runtime_snapshot_store = (
            runtime_snapshot_store or RuntimeSnapshotStore(self.sessions_root)
        )
        self.runtime_snapshot_builder = runtime_snapshot_builder
        self.runtime_pool_manager_factory = runtime_pool_manager_factory
        self.runtime_cleanup_inspector = runtime_cleanup_inspector
        self.runs: dict[str, Run] = {}
        self.provider_errors = ProviderErrorAggregator()
        self._seq = 0
        self.meta = RunMetaStore(root=self.sessions_root)
        # operator-created rail folders (id → name); runs reference one via meta.
        self.folders = FolderStore(root=self.sessions_root)
        # default worker-roster config (which engines launch per challenge); the
        # dispatch path falls back to this when a request doesn't say otherwise.
        self.worker_config = WorkerConfigStore(root=self.sessions_root)
        # pi-only convenience: mirror DEEPSEEK_API_KEY into pi-main so container
        # workers can mount it without forcing the operator to manage accounts.
        ensure_pi_account_from_env(self.sessions_root)
        # P4: FIFO run queue + global concurrency cap (default 5, 1..8). Every
        # manager start goes through it; the queue is in-memory (transient).
        from apps.web.run_scheduler import RunScheduler
        self.scheduler = RunScheduler(sessions_root=self.sessions_root)
        self._rehydrate()

    def _apply_meta(self, run: "Run") -> None:
        """Overlay persisted operator metadata (pin/archive/rename) onto a run."""
        m = self.meta.get(run.run_id)
        run.pinned = m["pinned"]
        run.pinned_at = m["pinned_at"]
        run.archived = m["archived"]
        run.custom_name = m["custom_name"]
        run.folder_id = m["folder_id"]
        run.sort_order = m["order"]

    def _rehydrate(self) -> None:
        """Re-populate the rail from durable JSONL on startup.

        Without this, a server restart drops every past conversation: self.runs
        starts empty so the rail shows nothing, AND _seq resets to 0 so the next
        "+ New solve" mints `run-0001` — colliding with a STALE run-0001.jsonl and
        replaying its old events under a "new" conversation. Hydrating both fixes
        history loss and the new-solve-shows-old-chat bug at once.

        We build lightweight Run handles (own bus + store) seeded with the
        persisted summary. The full event history is NOT loaded into memory here
        — the events SSE replays it from JSONL on demand. We only need the rail
        metadata + a correctly advanced _seq.
        """
        store = SessionStore(root=self.sessions_root)
        max_seq = 0
        # summaries() is newest-first; create() stamps created_seq in CALL order,
        # and the rail sorts by created_seq DESC — so feed oldest-first to keep the
        # newest conversation on top of the rail.
        for s in reversed(store.summaries()):
            rid = s["run_id"]
            # Skip never-dispatched drafts: a run that opened an SSE stream but was
            # never /start-ed has a JSONL with no run.started — it's an empty stub,
            # not a conversation. Don't let those clutter the rail on restart.
            if not s.get("started"):
                m0 = re.match(r"run-(\d+)$", rid)
                if m0:
                    max_seq = max(max_seq, int(m0.group(1)))
                continue
            run = self.create(rid, _defer_runtime_manager=True)
            # `summary()` falls back name→run_id; treat that as "no real title" so
            # the rail renders its placeholder instead of leaking the bare id.
            run.name = "" if s.get("name") in (None, "", rid) else s["name"]
            run.category = s.get("category", "") or ""
            run.started = bool(s.get("started"))
            # a rehydrated run has NO live task (the swarm coroutine died with the
            # previous server). So a started run is necessarily finished — even if
            # its on-disk summary says finished=False because it was killed mid-run
            # before emitting RUN_FINISHED (a "ghost run": the rail would otherwise
            # spin forever with no terminal event to settle it). Force-settle here.
            run.finished = bool(s.get("finished")) or run.started
            run.solved = bool(s.get("solved"))
            run.flag = s.get("flag")
            run.flags = list(s.get("flags") or ([run.flag] if run.flag else []))
            run.expected_flags = int(s.get("expected_flags") or 1)
            run.multi_flag = bool(s.get("multi_flag", False))
            # a rehydrated run is never live → it can't be paused or mid-run.
            run.paused = False
            # order persisted runs by recency of activity (newest gets the highest
            # created_seq, so the rail's reverse sort puts it on top). created_seq
            # is assigned by create() in call order; mirror it into updated_seq so
            # the "recent" section's recency sort matches on startup.
            run.updated_seq = run.created_seq
            run.updated_at = float(s.get("ts", 0.0) or 0.0)
            # overlay operator metadata (pin/archive/rename) from the side table.
            self._apply_meta(run)
            m = re.match(r"run-(\d+)$", rid)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        # advance the id counter past every persisted run-NNNN so create_new()
        # never re-mints an id that already has history on disk.
        self._seq = max(self._seq, max_seq)

    def get(self, run_id: str) -> Optional[Run]:
        return self.runs.get(run_id)

    def list_runs(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """Run summaries for the thread rail, newest first.

        Only STARTED runs are real conversations. A run handle also gets created
        lazily when a deck merely OPENS an SSE stream (so the stream is live the
        instant a run starts) — including for local draft ids that are never
        dispatched. Those empty stubs must not appear in the rail; the active
        draft is shown from the deck's own local state, not this list.

        Archived runs are hidden by default (the rail's "+ archived" view passes
        include_archived=True). Ordering: a RUNNING run always floats to the top
        (so the题 currently being solved is the first thing the operator sees —
        previously we sorted purely by created_seq, which buried a live run under
        already-finished ones when the manager rehydrated from disk in a different
        order than the eval ran). Within the running / non-running groups we sort
        by latest activity (updated_at), newest first, then created_seq as a tiebreak.
        """
        def _key(r: "Run"):
            running = r.status() == "running"
            return (1 if running else 0, r.updated_at or 0.0, r.created_seq)
        return [
            r.summary()
            for r in sorted(self.runs.values(), key=_key, reverse=True)
            if r.started and (include_archived or not r.archived)
        ]

    # ---- operator rail mutations (persisted in the meta side-table) ----------

    def set_pinned(self, run_id: str, pinned: bool, *, now: float) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        m = self.meta.set_pinned(run_id, pinned, now=now)
        run.pinned, run.pinned_at = m["pinned"], m["pinned_at"]
        return True

    def set_archived(self, run_id: str, archived: bool, *,
                     now: Optional[float] = None) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        m = self.meta.set_archived(run_id, archived,
                                   now=now if now is not None else time.time())
        run.archived, run.pinned, run.pinned_at = m["archived"], m["pinned"], m["pinned_at"]
        return True

    def rename(self, run_id: str, name: Optional[str]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.custom_name = self.meta.set_name(run_id, name)["custom_name"]
        return True

    def set_folder(self, run_id: str, folder_id: Optional[str]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.folder_id = self.meta.set_folder(run_id, folder_id)["folder_id"]
        return True

    def set_order(self, run_id: str, order: Optional[int]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.sort_order = self.meta.set_order(run_id, order)["order"]
        return True

    # ---- rail folders (operator-created groupings) ---------------------------

    def list_folders(self) -> list[dict[str, Any]]:
        return self.folders.list()

    def create_folder(self, name: str) -> dict[str, Any]:
        return self.folders.create(name)

    def update_folder(self, fid: str, *, name: Optional[str] = None,
                      order: Optional[int] = None) -> bool:
        return self.folders.update(fid, name=name, order=order)

    def delete_folder(self, fid: str) -> bool:
        # unfile every run that was in this folder, then drop the folder itself.
        self.meta.clear_folder_for_all(fid)
        for run in self.runs.values():
            if run.folder_id == fid:
                run.folder_id = None
        return self.folders.delete(fid)

    async def delete(self, run_id: str) -> bool:
        """Hard-delete a run: cancel its task(s), drop the handle + JSONL + meta."""
        run = self.runs.pop(run_id, None)
        if run is None:
            # still scrub any orphaned on-disk artifacts / meta
            self._delete_artifacts(run_id)
            return False
        # P4: a queued run has no task to cancel — drop it from the FIFO first
        # (the queue entry's driver closure is released with it).
        self.scheduler.cancel(run_id)
        # Cancel BOTH the swarm task and any live standby worker, then AWAIT them to
        # actually unwind before we close the bus / delete artifacts. Cancelling
        # without awaiting was a use-after-free race: the cancelled coroutine could
        # still be writing to the bus or reading an upload while we closed/removed
        # them. A cancelled task re-raises CancelledError on await — return_exceptions
        # swallows it (and any other shutdown error) so delete never self-destructs.
        pending = [t for t in (run.task, run.standby_task)
                   if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if run.pool_manager is not None:
            try:
                await run.pool_manager.close()
            except Exception:
                LOG.warning("runtime pool close failed while deleting run %s", run_id, exc_info=True)
        await run.bus.close()
        self._drop_board_schema(run_id)
        self._delete_artifacts(run_id)
        return True

    def _drop_board_schema(self, run_id: str) -> None:
        dsn = os.environ.get("DSWARM_BOARD_DSN", "").strip()
        if not dsn:
            return
        try:
            from dswarm.swarm.postgres_board import PostgresBoard

            PostgresBoard(dsn, challenge_id=run_id).drop_schema()
        except Exception:  # noqa: BLE001 - cleanup failure must not break delete
            LOG.warning("board schema cleanup failed for run %s", run_id)

    def _delete_artifacts(self, run_id: str) -> None:
        self.meta.forget(run_id)
        safe = run_id.replace("/", "_").replace("..", "_")
        jsonl = self.sessions_root / f"{safe}.jsonl"
        try:
            jsonl.unlink(missing_ok=True)
        except OSError:
            pass
        # also drop the per-run upload dir (sessions/{safe}/) so deleting a
        # conversation doesn't orphan its uploaded challenge files on disk.
        shutil.rmtree(self.sessions_root / safe, ignore_errors=True)

    # ---- retention sweep: auto-archive idle runs, then delete stale ones -----

    def _last_activity(self, run: "Run") -> float:
        """Epoch seconds of a run's most recent event (its idle clock). 0.0 when
        unknown (no persisted events) → such a run is never auto-touched."""
        try:
            return float(run.store.summary(run.run_id).get("ts", 0.0) or 0.0)
        except Exception:
            return 0.0

    async def retention_sweep(self, *, now: float, archive_after_s: float,
                              delete_after_s: float) -> dict[str, list[str]]:
        """One retention pass: archive started runs idle > archive_after_s, and
        DELETE already-archived runs idle > delete_after_s. PINNED runs are never
        auto-touched; runs with an unknown idle clock (ts==0) are skipped. Returns
        {"archived": [...], "deleted": [...]} for logging/tests."""
        archived: list[str] = []
        deleted: list[str] = []
        for run in list(self.runs.values()):
            if not run.started or run.pinned:
                continue
            ts = self._last_activity(run)
            if ts <= 0:
                continue  # can't date it → leave it alone
            idle = now - ts
            meta = self.meta.get(run.run_id)
            if meta["archived"]:
                if idle > delete_after_s:
                    await self.delete(run.run_id)
                    deleted.append(run.run_id)
                    LOG.info("retention: deleted stale archived run %s (idle %.0fs)",
                             run.run_id, idle)
            elif idle > archive_after_s:
                self.set_archived(run.run_id, True, now=now)
                archived.append(run.run_id)
                LOG.info("retention: archived idle run %s (idle %.0fs)", run.run_id, idle)
        return {"archived": archived, "deleted": deleted}

    async def retention_loop(self, *, interval_s: float, archive_after_s: float,
                             delete_after_s: float) -> None:
        """Background task: run retention_sweep every interval_s until cancelled.
        Sleeps FIRST so startup isn't blocked and a short-lived test process never
        triggers a sweep. A sweep failure is logged and the loop continues."""
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.retention_sweep(
                    now=time.time(), archive_after_s=archive_after_s,
                    delete_after_s=delete_after_s)
            except asyncio.CancelledError:
                break
            except Exception:
                LOG.exception("retention sweep failed; continuing")

    @staticmethod
    def _redact_dispatch_value(value: Any, key: str = "") -> Any:
        """Return JSON-safe dispatch settings without copying credentials.

        Dispatch profiles identify credential accounts, but a caller must never
        put raw API keys/tokens/passwords into the run workspace. The account
        store remains the source of truth for secrets.
        """
        secret_words = (
            "api_key", "apikey", "access_token", "refresh_token", "secret",
            "password", "passwd", "authorization", "private_key",
        )
        lowered = key.lower().replace("-", "_")
        if any(word in lowered for word in secret_words):
            return None
        if isinstance(value, dict):
            return {
                str(k): v
                for k, raw in value.items()
                if (v := RunManager._redact_dispatch_value(raw, str(k))) is not None
            }
        if isinstance(value, (list, tuple)):
            return [RunManager._redact_dispatch_value(v, key) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _strip_legacy_dispatch_fields(body: dict[str, Any]) -> dict[str, Any]:
        """Remove pre-v3 swarm knobs from a persisted recovery snapshot.

        ``build_driver`` intentionally rejects these fields for new requests. Older
        runs, however, were started while the knobs were still accepted and may
        have a sidecar containing them. Replaying that sidecar must not turn a
        valid historical run into an unrecoverable configuration error.
        """
        config = dict(body or {})
        for key in ("cli_race", "race_scout", "race_timeout", "race_engines",
                    "coordinator", "cold_start"):
            config.pop(key, None)
        stage = config.get("stage_policy")
        if isinstance(stage, dict):
            stage = dict(stage)
            stage.pop("race", None)
            stage.pop("coordinator", None)
            config["stage_policy"] = stage
        return config

    def remember_dispatch(self, run_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Persist the non-secret dispatch settings needed by ``resolve``.

        This is deliberately a sidecar, not a graph event: it is operator
        configuration, not solver evidence. Writes are atomic so a process crash
        cannot leave a partially-written config that makes recovery ambiguous.
        """
        run = self.runs.get(run_id) or self.create(run_id)
        safe = self._redact_dispatch_value(dict(body or {}))
        config = self._strip_legacy_dispatch_fields(
            safe if isinstance(safe, dict) else {}
        )
        run.dispatch_body = config
        path = self.workspace_dir(run_id) / ".dswarm_dispatch.json"
        try:
            import json
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            LOG.exception("could not persist dispatch settings for %s", run_id)
        return config

    def _load_dispatch(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is not None and run.dispatch_body:
            return self._strip_legacy_dispatch_fields(run.dispatch_body)
        path = self.sessions_root / run_id.replace("/", "_").replace("..", "_") / "workspace" / ".dswarm_dispatch.json"
        try:
            import json
            raw = json.loads(path.read_text(encoding="utf-8"))
            return self._strip_legacy_dispatch_fields(raw) if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    async def _infer_dispatch_from_history(self, run_id: str) -> dict[str, Any]:
        """Recover enough of a pre-sidecar dispatch to continue old runs.

        Sidecars were introduced after some runs already existed. For those runs,
        resolving with today's global roster can select a different profile (or a
        host-only health probe) and leave the reopened run with no worker. The
        event log contains the non-secret facts needed for a safe best-effort
        reconstruction: engines that actually came online and the runtime backend
        reported by those workers. Never infer credentials or arbitrary options.
        """
        engines: list[str] = []
        backend = ""
        online_workers: set[str] = set()
        try:
            async for ev in self.runs[run_id].store.replay(run_id):
                if ev.event_type is not EventType.WORKER_STATUS:
                    continue
                payload = ev.payload or {}
                if payload.get("online"):
                    engine = str(payload.get("engine") or "").strip()
                    if engine and engine not in engines:
                        engines.append(engine)
                    # worker.status is a heartbeat stream, not a spawn stream.
                    # Counting every online heartbeat inflated start_workers (run
                    # 1806 would recover with eight workers although only one was
                    # ever active). Prefer the durable solver id; old events without
                    # one fall back to the engine/role pair and therefore count once.
                    solver_id = str(getattr(ev, "solver_id", "") or "").strip()
                    role = str(payload.get("worker_role") or "").strip()
                    online_workers.add(solver_id or f"{engine}:{role}")
                runtime = payload.get("runtime")
                if isinstance(runtime, dict) and runtime.get("backend"):
                    backend = str(runtime["backend"]).strip()
        except Exception:
            return {}
        started_workers = len({key for key in online_workers if key.strip(":")})
        inferred: dict[str, Any] = {}
        if engines:
            # Historical worker events report the BASE engine (pi). Map it to the
            # run category's direction profile so an old-run resolve keeps the
            # single-worker behavior instead of expanding "pi" across all
            # direction profiles.
            from dswarm.solver.worker_profiles import direction_profile_name

            run = self.runs.get(run_id)
            category = str(getattr(run, "category", "") or "").strip()
            resolved: list[str] = []
            for engine in engines:
                if engine == "pi":
                    profile = direction_profile_name(category) or "pi-worker"
                else:
                    profile = engine
                if profile not in resolved:
                    resolved.append(profile)
            inferred["engines"] = resolved
        if backend in ("local", "container"):
            inferred["worker_backend"] = backend
        if started_workers:
            inferred["start_workers"] = min(started_workers, 8)
        return inferred

    def workspace_dir(self, run_id: str) -> Path:
        """Per-run persistent workspace: sessions/{id}/workspace/.

        Replaces the old tempfile.mkdtemp root so sandbox, artifacts, and
        shared_graph.db survive process restarts. Same id-sanitization as
        uploads_dir / _delete_artifacts."""
        safe = run_id.replace("/", "_").replace("..", "_")
        d = self.sessions_root / safe / "workspace"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def open_workspace(self, run_id: str) -> bool:
        """Open the run's workspace dir in the host file manager (operator-local —
        the deck runs in a browser, so a backend opener is the only way to truly
        reveal Finder/Explorer). Best-effort; False if it can't open."""
        import subprocess
        import sys

        d = self.workspace_dir(run_id)  # created if missing; opening empty is fine
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(d))  # type: ignore[attr-defined]
                return True
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            if shutil.which(opener) is None:
                return False
            subprocess.Popen([opener, str(d)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def uploads_dir(self, run_id: str) -> Path:
        """Per-run folder where uploaded challenge files land: sessions/{id}/uploads/.

        Each conversation gets its own directory so a file-based challenge's
        handouts stay scoped to that run (the worker later stages them into its
        cwd via CliSolver._stage_attachments). Sanitize the id with the same rule
        as _delete_artifacts so a hostile run_id can't escape sessions/. The dir
        is a sibling of the run's {id}.jsonl log — SessionStore only globs
        *.jsonl, so a directory of the same stem never collides with rehydration.
        """
        safe = run_id.replace("/", "_").replace("..", "_")
        d = self.sessions_root / safe / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def configure_budget(self, run_id: str, body: dict[str, Any] | None) -> None:
        """Apply explicit run budget settings without replacing usage projections."""
        run = self.runs.get(run_id)
        if run is None:
            return
        body = dict(body or {})
        nested = body.get("budget") if isinstance(body.get("budget"), dict) else {}
        profile_caps = body.get("profile_budget_caps", nested.get("profile_budget_caps"))
        account_caps = body.get("account_budget_caps", nested.get("account_budget_caps"))
        warn_ratio = body.get("budget_warn_ratio", nested.get("warn_ratio"))
        if isinstance(profile_caps, dict):
            run.budget_gate.profile_caps = {
                str(key): float(value) for key, value in profile_caps.items()
                if value is not None and float(value) >= 0
            }
        if isinstance(account_caps, dict):
            run.budget_gate.account_caps = {
                str(key): float(value) for key, value in account_caps.items()
                if value is not None and float(value) >= 0
            }
        if warn_ratio is not None:
            run.budget_gate.warn_ratio = min(1.0, max(0.0, float(warn_ratio)))
        # Re-apply durable projection records after cap configuration. The gate is
        # deliberately not recreated, so idempotent/action state remains intact.
        if run.ledger is not None:
            run.budget_gate.rebuild(
                run.ledger.records.values(), run.ledger.budget_actions
            )

    @staticmethod
    def _budget_alert_payload(run: Run, alert: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "budget_threshold",
            "run_id": run.run_id,
            **dict(alert),
        }

    async def _emit_budget_alert(self, run: Run, alert: dict[str, Any]) -> None:
        try:
            await run.bus.emit(Event(
                event_type=EventType.BUDGET_ALERT,
                run_id=run.run_id,
                payload=self._budget_alert_payload(run, alert),
            ))
        except Exception:
            LOG.debug("failed to emit budget alert for %s", run.run_id, exc_info=True)

    def _apply_accounting_event(self, run: Run, event: Event) -> tuple[dict[str, Any], ...]:
        """Fold canonical accounting events into the ledger and budget gate.

        The ledger remains the idempotency authority.  A duplicate canonical usage
        event therefore cannot double-charge the profile/account budget projection.
        Alert emission is deferred by the caller because this method runs inside a
        bus sink and must never recursively await ``bus.emit``.
        """
        if run.ledger is None:
            return ()
        if event.event_type is EventType.USAGE_RECORDED:
            accepted = run.ledger.apply_event(event)
            if not accepted:
                return ()
            record = UsageRecord(**dict(event.payload or {}))
            verdict = run.budget_gate.apply(record)
            return verdict.alerts
        if event.event_type is EventType.BUDGET_ACTION:
            run.ledger.apply_event(event)
            run.budget_gate.apply_action(dict(event.payload or {}))
        return ()

    def _schedule_budget_alerts(self, run: Run, alerts: tuple[dict[str, Any], ...]) -> None:
        for alert in alerts:
            task = asyncio.create_task(self._emit_budget_alert(run, alert))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def ensure_runtime_context(
        self,
        run_id: str,
        *,
        policy: RuntimePolicy,
        worker_profiles: Sequence[Mapping[str, Any]],
        runtime_profiles: Sequence[Mapping[str, Any]],
        run_max_workers: int,
        snapshot_builder: RuntimeSnapshotBuilder | None = None,
        pool_manager_factory: Callable[..., ContainerPoolManager] | None = None,
    ) -> tuple[RuntimePolicy, RuntimeSnapshot | None, ContainerPoolManager | None]:
        """Build or load exactly one immutable runtime context for ``run_id``."""
        if not isinstance(policy, RuntimePolicy):
            raise RuntimePolicyError("invalid_runtime_policy")
        run = self.create(run_id)
        if run.runtime_policy is not None:
            if run.runtime_snapshot is not None and run.pool_manager is None:
                factory = pool_manager_factory or self.runtime_pool_manager_factory
                if factory is not None:
                    run.pool_manager = factory(run_id=run_id, snapshot=run.runtime_snapshot)
            return run.runtime_policy, run.runtime_snapshot, run.pool_manager

        if policy.mode == "local_dev":
            if not policy.local_workers_allowed:
                raise RuntimePolicyError("local_worker_policy_denied")
            run.runtime_policy = policy
            return policy, None, None

        builder = snapshot_builder or self.runtime_snapshot_builder
        if builder is None:
            raise RuntimePolicyError("runtime_snapshot_builder_required")
        snapshot = builder.build(
            run_id=run_id,
            policy=policy,
            worker_profiles=worker_profiles,
            runtime_profiles=runtime_profiles,
            run_max_workers=run_max_workers,
        )
        if snapshot.run_id != run_id:
            raise RuntimePolicyError("runtime_snapshot_run_mismatch")
        if snapshot.runtime_policy != policy:
            raise RuntimePolicyError("runtime_policy_snapshot_mismatch")
        self.runtime_snapshot_store.create(snapshot)
        run.runtime_policy = snapshot.runtime_policy
        run.runtime_snapshot = snapshot
        factory = pool_manager_factory or self.runtime_pool_manager_factory
        if factory is not None:
            run.pool_manager = factory(run_id=run_id, snapshot=snapshot)
        return run.runtime_policy, run.runtime_snapshot, run.pool_manager

    async def _cleanup_before_reopen(self, run: Run) -> RuntimeCleanupResult | None:
        """Prove every stale private runtime is gone before a reopened dispatch."""
        inspector = self.runtime_cleanup_inspector
        if inspector is None:
            return None
        method = getattr(inspector, "cleanup_run_before_reopen", None)
        if not callable(method):
            raise RuntimeError("stale_runtime_cleanup_unproven")
        safe = run.run_id.replace("/", "_").replace("..", "_")
        run_root = self.sessions_root / safe
        result = method(run.run_id, run_root)
        if inspect.isawaitable(result):
            result = await result
        if not getattr(result, "proven", False):
            raise RuntimeError("stale_runtime_cleanup_unproven")
        return result

    def _ensure_runtime_pool_manager(self, run: Run) -> ContainerPoolManager | None:
        """Lazily construct the per-run pool after any reopen barrier."""
        if run.pool_manager is not None or run.runtime_snapshot is None:
            return run.pool_manager
        factory = self.runtime_pool_manager_factory
        if factory is None:
            return None
        run.pool_manager = factory(run_id=run.run_id, snapshot=run.runtime_snapshot)
        return run.pool_manager

    def create(self, run_id: str, *, _defer_runtime_manager: bool = False) -> Run:
        if run_id in self.runs:
            return self.runs[run_id]
        runtime_snapshot = None
        runtime_policy = None
        pool_manager = None
        snapshot_path = self.runtime_snapshot_store.path_for(run_id)
        if snapshot_path.is_file():
            runtime_snapshot = self.runtime_snapshot_store.load(run_id)
            runtime_policy = runtime_snapshot.runtime_policy
            if self.runtime_pool_manager_factory is not None and not _defer_runtime_manager:
                pool_manager = self.runtime_pool_manager_factory(
                    run_id=run_id, snapshot=runtime_snapshot
                )
        bus = EventBus()
        store = SessionStore(root=self.sessions_root)
        self._sync_bus_seq(bus, store=store, run_id=run_id)
        bus.add_critical_sink(store.sink, store.append_checked)
        self._seq += 1
        journal = UsageJournal(self.sessions_root / f"{run_id}-usage-journal.jsonl")
        ledger = UsageLedger(run_id=run_id)
        # Startup projection rebuild is synchronous and happens before a handle is
        # exposed to any spawn path. A failed replay leaves the guard failed so
        # stop/finalize remain available while new provider calls are rejected.
        try:
            ledger.rebuild(store.read_events(run_id), journal=journal)
        except Exception as exc:
            ledger.mark_failed(str(exc))
        guard = SpawnGuard()
        if ledger.state == "failed":
            guard.mark_failed(ledger.ledger_error or "ledger_rebuild_failed")
        elif ledger.pending_recovery_records():
            # Journal recovery is projected in memory first, then canonicalized
            # through the checked event path before any provider/spawn operation.
            guard.mark_rebuilding()
        run = Run(
            run_id=run_id, bus=bus, cost=CostController(bus=bus), store=store,
            usage_journal=journal, ledger=ledger, spawn_guard=guard,
            budget_gate=ProfileBudgetGate(),
            runtime_policy=runtime_policy,
            runtime_snapshot=runtime_snapshot,
            pool_manager=pool_manager,
            created_seq=self._seq,
            blackboard_token=secrets.token_urlsafe(32),
        )
        self._configure_gateway_bridge(run)
        # Restore the non-secret dispatch sidecar when this handle is created
        # after a backend restart. The helper is intentionally best-effort: old
        # runs simply have no sidecar and resolve falls back to their challenge.
        run.dispatch_body = self._load_dispatch(run_id)
        # sniff run.started / run.finished off the bus to keep rail metadata fresh
        # without making the run anything but a dumb event source.
        async def _meta_sink(ev: Event) -> None:
            # any event = activity. Keep this as metadata only: the rail itself is
            # creation-ordered, otherwise concurrent live runs visually hop around.
            self._seq += 1
            run.updated_seq = self._seq
            run.updated_at = ev.ts
            try:
                alerts = self._apply_accounting_event(run, ev)
                self._schedule_budget_alerts(run, alerts)
            except Exception as exc:
                if run.ledger is not None:
                    run.ledger.mark_failed(str(exc))
                if run.spawn_guard is not None:
                    run.spawn_guard.mark_failed(str(exc))
            if ev.event_type is EventType.RUN_STARTED:
                ch = ev.payload.get("challenge", {}) or {}
                run.started = True
                # Keep name EMPTY when the operator gave none — the rail renders a
                # "new conversation" placeholder, and the background summarizer fills
                # in a ChatGPT-style title via RUN_TITLED. Don't pin it to the run_id.
                if ch.get("name"):
                    run.name = ch["name"]
                run.category = ch.get("category", run.category) or run.category
                if ch.get("expected_flags"):
                    run.expected_flags = int(ch["expected_flags"])
                if "multi_flag" in ch:
                    run.multi_flag = bool(ch["multi_flag"])
            elif ev.event_type is EventType.RUN_TITLED:
                # auto-title landed from the background summarizer; only adopt it
                # if the operator hasn't supplied a real name (don't clobber).
                title = ev.payload.get("title") or ""
                if title and not run.name:
                    run.name = title
            elif ev.event_type is EventType.HITL_RESPONSE:
                # reflect pause/resume into the rail status icon. The driver still
                # owns the real halt; this is just the displayed state.
                action = ev.payload.get("action")
                if action == "pause":
                    run.paused = True
                elif action == "resume":
                    run.paused = False
                # ANY operator response lowers a raised hand. This MUST live in the
                # same branch: an if/elif chain only matches ONE HITL_RESPONSE arm,
                # so a separate `elif ev.event_type is HITL_RESPONSE` below was dead
                # code and the rail showed "需要输入" forever after a hint/answer.
                run.awaiting_help = False
                run.help_text = ""
            elif ev.event_type is EventType.RUN_REOPENED:
                # The run is solving again. Resolve/continue keeps all prior flags
                # visible; false-positive payloads carry the one invalid flag to
                # drop. Legacy false-positive payloads with no flag still clear all.
                run.finished = False
                run.solved = False
                run.paused = False
                if ev.payload.get("reason") == "resolve":
                    return
                bad = ev.payload.get("flag")
                if bad and run.flags:
                    run.flags = [f for f in run.flags if f != bad]
                    run.flag = run.flags[0] if run.flags else None
                else:
                    run.flag = None
                    run.flags = []
            elif ev.event_type is EventType.HITL_REQUEST:
                # a worker raised its hand (NEED_INPUT / env_down: target crashed,
                # instance expired, missing credential…). Surface it on the summary so
                # an operator (or a 1-min poll of /api/runs) sees it WITHOUT scanning
                # JSONL — this does NOT require the run to be "paused" (the swarm may
                # still be hurling workers at the wall while one hand is up).
                run.awaiting_help = True
                run.help_text = str((ev.payload or {}).get("need")
                                     or (ev.payload or {}).get("text") or "")[:300]
            elif ev.event_type is EventType.RUN_FINISHED:
                run.finished = True
                run.paused = False  # a finished run is never "paused"
                run.awaiting_help = False  # finished → no outstanding ask
                run.help_text = ""
                run.solved = bool(ev.payload.get("solved")) or run.solved
                run.merge_flags(ev.payload.get("flags") or ev.payload.get("flag"))
                if ev.payload.get("expected_flags"):
                    run.expected_flags = int(ev.payload["expected_flags"])
                if "multi_flag" in ev.payload:
                    run.multi_flag = bool(ev.payload["multi_flag"])
                if ev.payload.get("reason") == "cancelled":
                    run.cancelled = True
            elif ev.event_type is EventType.RUN_QUEUED:
                # P4: waiting for a scheduler slot — rail shows "queued (N)".
                run.queued = True
                run.queue_position = ev.payload.get("position")
                run.cancelled = False
            elif ev.event_type is EventType.RUN_DISPATCHED:
                # P4: the slot freed — the driver launches (RUN_STARTED follows).
                run.queued = False
                run.queue_position = None
            elif ev.event_type is EventType.RUN_CANCELLED:
                # P4: operator cancelled a queued run (RUN_FINISHED follows with
                # reason=cancelled — this is just the explicit marker).
                run.queued = False
                run.queue_position = None
                run.cancelled = True
            else:
                _apply_blackboard_meta(run, ev)

        bus.add_sink(_meta_sink)
        self.runs[run_id] = run
        # Restore caps/actions only after the Run is registered; configure_budget
        # resolves the live handle from self.runs during restart rehydration.
        self.configure_budget(run_id, run.dispatch_body)
        self._apply_meta(run)
        return run

    def _configure_gateway_bridge(self, run: Run) -> None:
        """Attach the process-wide gateway to this run's owner loop and bus.

        The HTTP gateway serves multiple runs from worker threads, while each run
        owns its own asyncio EventBus. Registration is keyed by ``run_id`` and is
        repeated when a closed bus is rebuilt. Synchronous maintenance callers do
        not have an owner loop yet, so registration is deferred for that path.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from dswarm.solver.modelgateway import ModelGateway
        from dswarm.solver.credential_accounts import account_store_root

        gateway = ModelGateway.instance()
        gateway.account_root = str(account_store_root(self.sessions_root))
        gateway.sessions_root = str(self.sessions_root)
        gateway.configure_usage_bridge(bus=run.bus, loop=loop, run_id=run.run_id)

    def internal_usage_writer(
        self,
        run: Run,
        *,
        solver_id: str | None = None,
        profile_id: str | None = None,
        configured_account_id: str | None = None,
        worker_instance_id: str | None = None,
    ) -> UsageWriter:
        """Create an internal-producer writer sharing this run's journal."""
        journal = getattr(run, "usage_journal", None) or UsageJournal(
            self.sessions_root / f"{run.run_id}-usage-journal.jsonl"
        )
        run.usage_journal = journal
        return UsageWriter(
            journal,
            bus=run.bus,
            context=UsageContext(
                run_id=run.run_id,
                challenge_id=run.run_id,
                worker_instance_id=worker_instance_id,
                solver_id=solver_id,
                profile_id=profile_id,
                configured_account_id=configured_account_id,
                billing_account_id=None,
                producer="internal",
            ),
        )

    def fallback_usage_writer(
        self,
        run: Run,
        *,
        solver_id: str | None = None,
        profile_id: str | None = None,
        worker_instance_id: str | None = None,
    ) -> UsageWriter:
        """Create the non-gateway CLI invocation-aggregate writer."""
        journal = getattr(run, "usage_journal", None) or UsageJournal(
            self.sessions_root / f"{run.run_id}-usage-journal.jsonl"
        )
        run.usage_journal = journal
        return UsageWriter(
            journal,
            bus=run.bus,
            context=UsageContext(
                run_id=run.run_id,
                challenge_id=run.run_id,
                worker_instance_id=worker_instance_id,
                solver_id=solver_id,
                profile_id=profile_id,
                configured_account_id=None,
                billing_account_id=None,
                producer="fallback",
            ),
        )

    def board_token(self, run_id: str) -> str:
        run = self.get(run_id)
        return run.blackboard_token if run else ""

    def verify_board_token(self, run_id: str, token: str) -> bool:
        expected = self.board_token(run_id)
        return bool(expected) and secrets.compare_digest(expected, str(token or ""))

    def archive_legacy_runs(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Archive old SQLite runs under sessions/_archive before cleanup.

        The operation is intentionally conservative: each run is archived to a
        tar.gz, the archive is opened and validated, and only then is the original
        run directory removed. Any failure leaves the source data in place.
        """
        archive_root = self.sessions_root / "_archive"
        archived: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for path in sorted(self.sessions_root.iterdir()):
            name = path.name
            if not path.is_dir() or name.startswith("_"):
                continue
            if name in self.runs:
                run = self.runs[name]
                if run.started and not run.finished:
                    skipped.append(f"{name}:active")
                    continue
            graph_db = path / "graph" / "shared_graph.db"
            jsonl = self.sessions_root / f"{name}.jsonl"
            if not graph_db.exists() and not jsonl.exists():
                skipped.append(f"{name}:no_data")
                continue
            if dry_run:
                archived.append(name)
                continue
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_path = archive_root / f"{name}.tar.gz"
            if archive_path.exists():
                skipped.append(f"{name}:archive_exists")
                continue
            try:
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(path, arcname=name)
                    if jsonl.exists():
                        tar.add(jsonl, arcname=f"{name}.jsonl")
                with tarfile.open(archive_path, "r:gz") as check:
                    if not check.getnames():
                        raise RuntimeError("archive is empty")
                shutil.rmtree(path)
                if jsonl.exists():
                    jsonl.unlink()
                archived.append(name)
            except Exception as exc:  # noqa: BLE001 - cleanup must not lose data
                failed.append({"run_id": name, "error": str(exc)})
                if archive_path.exists():
                    try:
                        archive_path.unlink()
                    except OSError:
                        pass
        return {
            "dry_run": dry_run,
            "archived": archived,
            "skipped": skipped,
            "failed": failed,
        }

    @staticmethod
    def _bump_bus_seq(bus: EventBus, seq: int) -> None:
        try:
            bus._seq = max(int(getattr(bus, "_seq", 0) or 0), int(seq or 0))
        except Exception:
            pass

    def _sync_bus_seq(
        self, bus: EventBus, *, store: Optional[SessionStore] = None,
        run_id: str
    ) -> None:
        store = store or SessionStore(root=self.sessions_root)
        self._bump_bus_seq(bus, store.last_stream_seq(run_id))

    def create_new(self) -> Run:
        """Mint a run under a fresh, never-reused id (for '+ New solve')."""
        self._seq += 1
        run_id = f"run-{self._seq:04d}"
        while run_id in self.runs:
            self._seq += 1
            run_id = f"run-{self._seq:04d}"
        return self.create(run_id)

    async def rebuild_ledger(self, run_id: str) -> Run:
        """Replay canonical usage and reconcile journal terminals on demand.

        This is the explicit operator recovery path behind the budget snapshot
        UI.  It uses the same all-or-fail sequence as startup and pre-spawn
        reconciliation: replay first, project the budget gate, then canonicalize
        journal-only terminals through the checked event path.
        """
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.ledger is None or run.usage_journal is None or run.store is None:
            raise RuntimeError("ledger_unavailable")
        if run.spawn_guard is not None:
            run.spawn_guard.mark_rebuilding()
        try:
            run.ledger.rebuild(
                run.store.read_events(run_id),
                journal=run.usage_journal,
            )
            run.budget_gate.rebuild(
                run.ledger.records.values(),
                run.ledger.budget_actions,
            )
            pending = run.ledger.pending_recovery_records()
            for record in pending:
                await run.bus.emit_checked(Event(
                    event_type=EventType.USAGE_RECORDED,
                    run_id=run.run_id,
                    payload=record.__dict__.copy(),
                ))
            run.ledger.mark_reconciled(record.usage_id for record in pending)
            if run.spawn_guard is not None:
                run.spawn_guard.mark_ready()
            return run
        except Exception as exc:
            run.ledger.mark_failed(str(exc))
            if run.spawn_guard is not None:
                run.spawn_guard.mark_failed(str(exc))
            raise

    async def _reconcile_ledger(self, run: Run) -> None:
        """Canonicalize journal-only terminals before allowing a spawn."""
        if run.ledger is None:
            return
        pending = run.ledger.pending_recovery_records()
        if not pending:
            if run.spawn_guard is not None and run.spawn_guard.ledger_state == "rebuilding":
                run.spawn_guard.mark_ready()
            return
        if run.spawn_guard is not None:
            run.spawn_guard.mark_rebuilding()
        try:
            for record in pending:
                await run.bus.emit_checked(Event(
                    event_type=EventType.USAGE_RECORDED,
                    run_id=run.run_id,
                    payload=record.__dict__.copy(),
                ))
            run.ledger.mark_reconciled(record.usage_id for record in pending)
            if run.spawn_guard is not None:
                run.spawn_guard.mark_ready()
        except Exception as exc:
            run.ledger.mark_failed(str(exc))
            if run.spawn_guard is not None:
                run.spawn_guard.mark_failed(str(exc))
            raise

    async def start(self, run_id: str, driver: Driver) -> Run:
        """Dispatch a run through the P4 scheduler: below the concurrency cap it
        launches immediately (today's behavior); at the cap it enters the FIFO
        queue (RUN_QUEUED with its position) and starts when a slot frees.

        A run that is ALREADY live (task pending) is not double-dispatched —
        the existing handle is returned untouched.
        """
        run = self.create(run_id)
        if run.task is not None and not run.task.done():
            return run  # already live — never stack a second driver on one run
        await self._reconcile_ledger(run)
        if run.runtime_snapshot is not None and run.finished:
            await self._cleanup_before_reopen(run)
        self._ensure_runtime_pool_manager(run)
        if run.spawn_guard is not None:
            await run.spawn_guard.ensure_ready(run_id)
        run.started = True  # the operator dispatched it; rail shows queued/running
        position = self.scheduler.submit(run_id, driver)
        if position is None:
            # slot was free and is now held by this run — launch right away.
            self._launch(run, driver)
            return run
        run.queued = True
        run.queue_position = position
        await run.bus.emit(Event(
            event_type=EventType.RUN_QUEUED, run_id=run_id,
            payload={"position": position,
                     "active": self.scheduler.active_count,
                     "limit": self.scheduler.max_concurrent_runs}))
        return run

    async def _emit_provider_diagnostics(
        self, run: "Run", detail: str, *, worker_id: str = "", provider: str = "",
        account_id: str = "", active_workers: int | None = None,
    ) -> None:
        """Emit operator-facing diagnostics for a provider/runtime failure.

        This does not verify/accept flags and does not alter solver state; it only
        translates raw LLM/provider/CLI error text into stable events the UI can
        surface. Fatal quota/auth/model errors are visible immediately as
        ``provider.error``; repeated failures in the sliding window additionally
        produce ``provider.batch_alert`` so the operator knows dispatch should be
        paused or reconfigured.
        """
        raw = str(detail or "").strip()
        if not raw:
            return
        diag = classify_provider_error(
            raw, provider=provider, account_id=account_id,
            worker_id=worker_id or run.run_id,
        )
        try:
            await run.bus.emit(Event(
                event_type=EventType.PROVIDER_ERROR,
                run_id=run.run_id,
                solver_id=worker_id or None,
                payload=diag.to_event(),
            ))
        except Exception:
            LOG.exception("failed to emit provider diagnostic for run %s", run.run_id)
            return
        try:
            active = active_workers
            if active is None:
                active = max(self.scheduler.active_count, 1)
            alert = self.provider_errors.record(
                diag, now=time.time(), active_workers=int(active or 0),
            )
            if alert:
                await run.bus.emit(Event(
                    event_type=EventType.PROVIDER_BATCH_ALERT,
                    run_id=run.run_id,
                    payload=alert,
                ))
        except Exception:
            LOG.exception("failed to aggregate provider diagnostic for run %s", run.run_id)

    def _launch(self, run: "Run", driver: Driver) -> None:
        """Create the run's driver task (slot already held by the scheduler).
        The driver's finally frees the slot and fills the queue."""
        run_id = run.run_id
        run.queued = False
        run.queue_position = None

        async def _go() -> None:
            failure_detail = ""
            try:
                await driver(run)
            except Exception as exc:
                LOG.exception("driver crashed for run %s", run_id)
                failure_detail = str(exc)[:500]
                await self._emit_provider_diagnostics(run, failure_detail)
            finally:
                # If the driver exited WITHOUT emitting RUN_FINISHED (cancelled
                # mid-run, or it crashed before its own terminal event), the deck
                # never gets a terminal signal and the rail spins forever (a "ghost
                # run"). _meta_sink flips run.finished=True on a real RUN_FINISHED,
                # so a still-False flag here means none was emitted — synthesize one
                # before closing the bus so every run reaches a settled state.
                if not run.finished:
                    try:
                        await run.bus.emit(Event(
                            event_type=EventType.RUN_FINISHED, run_id=run_id,
                            payload={"flag": run.flag, "flags": list(run.flags),
                                     "expected_flags": run.expected_flags,
                                     "multi_flag": run.multi_flag,
                                     "solved": run.solved,
                                     "reason": "runtime_failure",
                                     "detail": failure_detail}))
                    except Exception:
                        pass
                run.finished = True
                # P4: the slot freed — hand the queue the chance to start the next
                # run BEFORE closing this bus (launch is independent of it).
                self.scheduler.release(run_id)
                await self._fill_slots()
                await run.bus.close()
                self._unregister_gateway_bridge(run_id)

        run.task = asyncio.create_task(_go())

    # ── P4 scheduler plumbing ─────────────────────────────────────────────────
    async def _fill_slots(self) -> None:
        """Launch every queued run a freed slot can hold, FIFO (held/paused
        queued runs are skipped but keep their position). Called from a driver's
        finally, a limit raise, and queued-run resume."""
        while True:
            # never over-subscribe: only pop while a slot is actually free.
            if self.scheduler.active_count >= self.scheduler.max_concurrent_runs:
                return
            nxt = self.scheduler.next_to_dispatch()
            if nxt is None:
                return
            rid, _driver = nxt
            run = self.runs.get(rid)
            if run is None:
                self.scheduler.release(rid)  # vanished (deleted) while queued
                continue
            await run.bus.emit(Event(
                event_type=EventType.RUN_DISPATCHED, run_id=rid,
                payload={"active": self.scheduler.active_count,
                         "limit": self.scheduler.max_concurrent_runs}))
            # the emit above yields the loop — the operator may have DELETED this
            # run in the meantime. Never launch a driver on a deleted handle.
            if self.runs.get(rid) is not run:
                self.scheduler.release(rid)
                continue
            self._launch(run, _driver)

    async def cancel_queued(self, run_id: str) -> bool:
        """Operator cancels a run that is STILL WAITING in the queue: drop it
        from the FIFO, settle the run (cancelled + RUN_FINISHED so the rail and
        the event stream get a terminal state). False when it wasn't queued."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.cancel(run_id):
            return False
        run.queued = False
        run.queue_position = None
        run.cancelled = True
        await run.bus.emit(Event(
            event_type=EventType.RUN_CANCELLED, run_id=run_id,
            payload={"reason": "operator_cancelled_queued"}))
        await run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED, run_id=run_id,
            payload={"flag": run.flag, "flags": list(run.flags),
                     "expected_flags": run.expected_flags,
                     "multi_flag": run.multi_flag,
                     "solved": False, "reason": "cancelled"}))
        await run.bus.close()
        return True

    async def cancel_run(self, run_id: str) -> bool:
        """P4 'cancel': a queued run is dropped from the FIFO; a LIVE run gets
        the existing graceful stop (operator_stop, workers killed, state kept).
        False for an unknown/finished run."""
        run = self.runs.get(run_id)
        if run is None:
            return False
        if self.scheduler.is_queued(run_id):
            return await self.cancel_queued(run_id)
        if run.task is not None and not run.task.done():
            return await self.post_hitl(run_id, "global", "stop")
        return False

    async def pause_queued(self, run_id: str) -> bool:
        """Hold a queued run: it keeps its queue position but is skipped by
        dispatch until resumed. Emits the same HITL_RESPONSE pause the live-run
        path uses, so the rail's paused icon works unchanged."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.pause(run_id):
            return False
        run.paused = True
        await run.bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload("global", "pause")))
        return True

    async def resume_queued(self, run_id: str) -> bool:
        """Un-hold a queued run and give the scheduler a chance to dispatch it."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.resume(run_id):
            return False
        run.paused = False
        await run.bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload("global", "resume")))
        await self._fill_slots()
        return True

    def scheduler_snapshot(self) -> dict[str, Any]:
        """BTFly-style SchedulerStatus: settings + active count + FIFO queue with
        per-run title/category enriched from the live run handles."""
        s = self.scheduler
        queue = []
        for rid, pos, at in s.queued_entries():
            run = self.runs.get(rid)
            queue.append({
                "run_id": rid,
                "title": (run.name or rid) if run is not None else rid,
                "category": (run.category or "") if run is not None else "",
                "position": pos,
                "queued_at": at,
            })
        return {
            "settings": {"max_concurrent_runs": s.max_concurrent_runs},
            "active_count": s.active_count,
            "queued_count": s.queued_count,
            "queue": queue,
        }

    def set_scheduler_limit(self, n: int) -> int:
        """Clamp (1..8) + persist the concurrency limit; returns the effective
        value. A raise is dispatched by the caller via dispatch_pending()."""
        return self.scheduler.set_limit(n)

    async def dispatch_pending(self) -> None:
        """Give the scheduler a chance to fill slots (limit raise / resume)."""
        await self._fill_slots()

    # actions a standby (post-solve) worker can serve. pause/resume/submit only
    # make sense against a LIVE run, so they never trigger a standby.
    _STANDBY_ACTIONS = {"ask", "hint", "mark_false", "writeup", "redirect", "focus"}
    async def post_hitl(self, run_id: str, target: str, action: str, **fields: Any) -> bool:
        """Route a human command into the run + echo it on the event stream.

        While the run is LIVE, the command flows to the running swarm via run.hitl
        (pause/resume act on the subprocess; hints reach workers). Once the run has
        FINISHED — or the server restarted and there's no live task — a follow-up
        would otherwise vanish: nothing drains run.hitl. So we COLD-START a standby
        worker (resume the winner's session) to actually respond."""
        run = self.runs.get(run_id)
        if run is None:
            return False
        # stop: gracefully END a run. A LIVE run: cancel run.task → the swarm's
        # finally-block _cancel_solver + killpg every worker, the driver's finally
        # closes the bus → the run reaches `finished` (RUN_FINISHED), JSONL + board
        # PRESERVED (unlike DELETE). A GHOST run (no live task but the deck still
        # shows "running" because its event stream ended mid-flight without a
        # terminating RUN_FINISHED — e.g. a relaunch killed when the server died,
        # run-4305): we FORCE it finished here + broadcast RUN_FINISHED so the deck
        # settles and shows the finished controls. Stop must never leave a run stuck.
        if action == "stop":
            await run.bus.emit(Event(
                event_type=EventType.HITL_RESPONSE, run_id=run_id,
                payload=hitl_response_payload(target, action, **fields)))
            if run.task is not None and not run.task.done():
                # ⑤ Route stop THROUGH the hitl queue first so the swarm's _drain_hitl
                # sets _operator_stop=True and the coordinator finalizes as
                # "operator_stop" — NOT "runtime_failure". A bare task.cancel() (the old
                # path) skipped that flag, so finalize mislabeled an operator stop as a
                # crash and parked every in-flight intent as resume noise (run-75377: 53
                # stranded intents). Give the coordinator a brief window to drain + exit
                # cleanly on its own; cancel only as a backstop if it doesn't.
                try:
                    run.hitl.put_nowait({"action": "stop", "target": target})
                except Exception:
                    pass
                for _ in range(40):  # ~4s: _drain_hitl runs each coordinator tick
                    await asyncio.sleep(0.1)
                    if run.task.done():
                        break
                if not run.task.done():
                    run.task.cancel()
            else:
                # ghost / already-dead task → settle the state ourselves.
                run.finished = True
                run.paused = False
                await run.bus.emit(Event(
                    event_type=EventType.RUN_FINISHED, run_id=run_id,
                    payload={"flag": run.flag, "flags": list(run.flags),
                             "expected_flags": run.expected_flags,
                             "multi_flag": run.multi_flag,
                             "solved": run.solved,
                             "reason": "operator_stop"}))
            return True
        # M2: drop an identical back-to-back resend (same target/action/text/url).
        # The UI has no client throttle, and an operator hammering the SAME hint at a
        # busy single-shot worker (run-0011: 11×) otherwise queues 11 items + 11
        # events + 11 downstream _drain_hitl sweeps. A genuinely new command (changed
        # text, or a different action) still goes through.
        sig = (target, action, str(fields.get("text") or fields.get("hint") or ""),
               str(fields.get("url") or fields.get("target_url") or ""),
               str(fields.get("flag") or ""))
        # `writeup` is an idempotent-looking no-arg command from the UI, but each
        # click is a real request to run a fresh post-solve standby turn. If we
        # dedupe it here, the second "生成复盘" click only echoes a duplicate
        # HITL_RESPONSE and never starts a worker, which reads as a stuck button.
        if action != "writeup" and getattr(run, "_last_hitl_sig", None) == sig:
            await run.bus.emit(Event(
                event_type=EventType.HITL_RESPONSE, run_id=run_id,
                payload=hitl_response_payload(target, action, delivery="duplicate",
                                              **fields)))
            return True
        run._last_hitl_sig = sig

        live = run.task is not None and not run.task.done()
        # M4: tell the operator WHERE the command went, so they stop re-sending a hint
        # that already landed. A non-standing hint can't steer a live single-shot
        # worker mid-turn (it's folded into the NEXT spawn), and a finished run routes
        # to a cold-start standby — both look identical without this status.
        if live:
            delivery = "queued_for_next_worker" if action in ("hint", "focus") else "applied_live"
        elif action in self._STANDBY_ACTIONS:
            delivery = "standby"
        else:
            delivery = "no_live_workers"

        await run.hitl.put({"target": target, "action": action, **fields})
        await run.bus.emit(
            Event(
                event_type=EventType.HITL_RESPONSE,
                run_id=run_id,
                payload=hitl_response_payload(target, action, delivery=delivery, **fields),
            )
        )
        if not live and action in self._STANDBY_ACTIONS:
            self._ensure_standby(run_id, {"target": target, "action": action, **fields})
        return True

    async def post_worker_cmd(self, run_id: str, action: str, *,
                              engine: Optional[str] = None,
                              solver_id: Optional[str] = None) -> bool:
        """Queue an operator worker command (spawn/kill) for the LIVE coordinator
        to drain. Only meaningful while the run is running; a finished/ghost run
        has no coordinator loop to act on it, so we reject it."""
        run = self.runs.get(run_id)
        if run is None:
            return False
        live = run.task is not None and not run.task.done()
        if not live:
            return False
        cmd: dict[str, Any] = {"action": action}
        if engine:
            cmd["engine"] = engine
        if solver_id:
            cmd["solver_id"] = solver_id
        await run.worker_cmds.put(cmd)
        return True

    async def resolve(self, run_id: str, body: dict[str, Any] | None = None) -> bool:
        """Continue a finished run through the normal scheduler/launch path.

        Recovery must be indistinguishable from a normal dispatch after the
        ``RUN_REOPENED`` marker: it gets a scheduler slot, uses ``_launch`` for
        exception handling/terminal events, and restores the original dispatch
        settings. The old implementation created an untracked task directly; a
        preflight/worker exception therefore produced only ``run.reopened`` and
        silently closed the bus, leaving the UI claiming that the swarm was back
        while no worker ever came online.
        """
        run = self.runs.get(run_id)
        if run is None:
            return False
        if run.task is not None and not run.task.done():
            return False  # already live — nothing to relaunch (use HITL instead)
        # A previous failed recovery must not leave a scheduler slot or queue entry
        # that makes the next click look successful but never dispatch.
        if self.scheduler.is_active(run_id) or self.scheduler.is_queued(run_id):
            return False
        await self._reconcile_ledger(run)
        if run.runtime_snapshot is not None:
            await self._cleanup_before_reopen(run)
        self._ensure_runtime_pool_manager(run)
        if run.spawn_guard is not None:
            await run.spawn_guard.ensure_ready(run_id)

        # Reconstruct the challenge from the durable winner/event history, then
        # layer the saved dispatch body underneath it. This preserves custom
        # engines/profiles/backend across a server restart instead of silently
        # using today's global settings.
        ch: dict[str, Any] = {}
        try:
            import json
            wp = self.workspace_dir(run_id) / "winner.json"
            if wp.exists():
                ch = (json.loads(wp.read_text(encoding="utf-8")) or {}).get("challenge") or {}
        except Exception:
            ch = {}
        if not ch:
            try:
                async for ev in run.store.replay(run_id):
                    if ev.event_type is EventType.RUN_STARTED:
                        ch = (ev.payload or {}).get("challenge") or {}
                        # Keep looking: multiple workers emit run.started, but the
                        # challenge payload is equivalent and the first is enough.
                        break
            except Exception:
                ch = {}
        saved = self._load_dispatch(run_id)
        if not saved:
            # Compatibility for runs created before .dswarm_dispatch.json existed.
            # Prefer the roster/backend that really produced prior worker events over
            # whatever global worker settings happen to be active today.
            saved = await self._infer_dispatch_from_history(run_id)
        saved_ch = saved.get("challenge") if isinstance(saved.get("challenge"), dict) else {}
        if not ch:
            ch = dict(saved_ch)
        if not ch:
            ch = {"name": run.name or run_id, "category": run.category or "web",
                  "expected_flags": run.expected_flags,
                  "multi_flag": run.multi_flag}
        merged = merge_resolve_dispatch(saved, body, historical_challenge=ch)

        # Continue directly in the coordinator on the existing evidence graph.
        # Do not inject the former race_scout/cold_start knobs here: current
        # build_driver deliberately rejects those legacy fields. Reusing the same
        # workspace already gives the coordinator its existing graph context.

        self.configure_budget(run_id, merged)
        # Build synchronously before changing lifecycle state. Configuration errors
        # should be returned by /resolve, not recorded as a misleading reopen.
        from apps.web.drivers import build_driver
        driver = build_driver(
            merged,
            mgr=self,
            runtime_operation_kind="resolve",
        )
        self.remember_dispatch(run_id, merged)

        # Reopen the bus and reset every lifecycle bit that can make the rail show a
        # stale terminal/queued/cancelled state. Keep old flags as deliberate
        # evidence carried into the continuation.
        self._fresh_bus(run)
        run.started = True
        run.finished = False
        run.solved = False
        run.paused = False
        run.queued = False
        run.queue_position = None
        run.cancelled = False
        run.awaiting_help = False
        run.help_text = ""
        await run.bus.emit(Event(
            event_type=EventType.RUN_REOPENED, run_id=run_id,
            payload={"reason": "resolve"}))

        position = self.scheduler.submit(run_id, driver)
        if position is None:
            # _launch owns the task, catches driver failures, emits a terminal
            # runtime_failure, releases the slot, and closes the bus.
            self._launch(run, driver)
        else:
            run.queued = True
            run.queue_position = position
            await run.bus.emit(Event(
                event_type=EventType.RUN_QUEUED, run_id=run_id,
                payload={"position": position,
                         "active": self.scheduler.active_count,
                         "limit": self.scheduler.max_concurrent_runs}))
        return True

    def _unregister_gateway_bridge(self, run_id: str) -> None:
        """Detach a finished run from the process-wide gateway bridge.

        Usage remains recoverable from the run-scoped journal; this only prevents
        late gateway callbacks from targeting a closed EventBus. Standby/resolve
        paths call ``_fresh_bus`` and register the run again before spawning.
        """
        try:
            from dswarm.solver.modelgateway import ModelGateway
            unregister = getattr(ModelGateway.instance(), "unregister_usage_bridge", None)
            if callable(unregister):
                unregister(run_id)
        except Exception:
            LOG.debug("failed to unregister gateway bridge for %s", run_id, exc_info=True)

    def _fresh_bus(self, run: Run) -> None:
        """Replace a run's CLOSED bus with a live one (same sinks) so a standby
        worker's events reach a freshly-opened SSE stream. After the main run
        ended, run.bus was close()d — its subscribers got the end sentinel and the
        browser's EventSource reconnected, but the closed bus won't fan out to new
        subscribers. A new bus, re-wired to the SessionStore + rail meta sinks,
        keeps the durable JSONL append-only and the rail metadata fresh."""
        durable_seq = run.store.last_stream_seq(run.run_id)
        self._bump_bus_seq(run.bus, durable_seq)
        if not getattr(run.bus, "_closed", False):
            return  # still open (live run) — keep it
        new_bus = EventBus()
        new_bus.add_critical_sink(run.store.sink, run.store.append_checked)
        new_bus.add_sink(self._meta_sink_for(run))
        # carry the seq forward so SSE Last-Event-ID continuity holds across runs
        self._bump_bus_seq(new_bus, max(getattr(run.bus, "_seq", 0), durable_seq))
        run.bus = new_bus
        run.cost.bus = new_bus  # cost updates emit onto the live bus too
        self._configure_gateway_bridge(run)

    def _ensure_standby(self, run_id: str, cmd: dict[str, Any]) -> None:
        """Spin up a standby worker to serve `cmd`, unless one is already running
        (serialized — one standby per run). Fire-and-forget; events stream live."""
        run = self.runs.get(run_id)
        if run is None:
            return
        if run.standby_task is not None and not run.standby_task.done():
            return  # a standby is already serving this run — don't pile on
        self._fresh_bus(run)
        from apps.web.drivers import build_standby_driver
        driver = build_standby_driver(cmd, mgr=self)

        async def _go() -> None:
            try:
                await self._reconcile_ledger(run)
                if run.spawn_guard is not None:
                    await run.spawn_guard.ensure_ready(run_id)
                LOG.info("standby worker starting for %s action=%s",
                         run_id, cmd.get("action"))
                await driver(run)
                LOG.info("standby worker finished for %s action=%s",
                         run_id, cmd.get("action"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc)[:500]
                LOG.exception("standby worker failed for %s action=%s",
                              run_id, cmd.get("action"))
                try:
                    await run.bus.emit(Event(
                        event_type=EventType.HITL_REQUEST,
                        run_id=run_id,
                        payload={
                            "target": cmd.get("target") or "global",
                            "source": "standby",
                            "action": cmd.get("action"),
                            "need": f"standby worker failed: {detail}",
                            "text": f"standby worker failed: {detail}",
                        },
                    ))
                except Exception:
                    pass
            finally:
                # do NOT close the bus — keep the run reachable for more follow-ups.
                run.standby_task = None

        run.standby_task = asyncio.create_task(_go())

    def _meta_sink_for(self, run: Run):
        """The rail-metadata sink bound to a specific Run (used when rebuilding a
        fresh bus). Mirrors the inline _meta_sink in create()."""
        async def _meta_sink(ev: Event) -> None:
            self._seq += 1
            run.updated_seq = self._seq
            run.updated_at = ev.ts
            try:
                alerts = self._apply_accounting_event(run, ev)
                self._schedule_budget_alerts(run, alerts)
            except Exception as exc:
                if run.ledger is not None:
                    run.ledger.mark_failed(str(exc))
                if run.spawn_guard is not None:
                    run.spawn_guard.mark_failed(str(exc))
            if ev.event_type is EventType.RUN_REOPENED:
                run.finished = False
                run.solved = False
                run.paused = False
                if ev.payload.get("reason") == "resolve":
                    return
                bad = ev.payload.get("flag")
                if bad and run.flags:
                    run.flags = [f for f in run.flags if f != bad]
                    run.flag = run.flags[0] if run.flags else None
                else:
                    run.flag = None
                    run.flags = []
            elif ev.event_type is EventType.HITL_REQUEST:
                # a (standby) worker raised its hand — surface it on the summary, same
                # as the inline _meta_sink in create().
                run.awaiting_help = True
                run.help_text = str((ev.payload or {}).get("need")
                                     or (ev.payload or {}).get("text") or "")[:300]
            elif ev.event_type is EventType.HITL_RESPONSE:
                # mirror the primary sink: reflect pause/resume AND lower the hand.
                action = ev.payload.get("action")
                if action == "pause":
                    run.paused = True
                elif action == "resume":
                    run.paused = False
                run.awaiting_help = False
                run.help_text = ""
            elif ev.event_type is EventType.RUN_FINISHED:
                run.finished = True
                run.paused = False
                run.awaiting_help = False
                run.help_text = ""
                run.solved = bool(ev.payload.get("solved")) or run.solved
                run.merge_flags(ev.payload.get("flags") or ev.payload.get("flag"))
                if ev.payload.get("expected_flags"):
                    run.expected_flags = int(ev.payload["expected_flags"])
                if "multi_flag" in ev.payload:
                    run.multi_flag = bool(ev.payload["multi_flag"])
            else:
                _apply_blackboard_meta(run, ev)
        return _meta_sink

    async def shutdown(self) -> None:
        """Cancel every live task on server shutdown so no swarm/standby coroutine —
        and its shelled CLI subprocess group — survives as a budget-eating zombie.
        Cancels BOTH run.task AND standby_task (the latter was leaking: a standby
        worker spun up to answer a post-solve follow-up kept running). The titler is a
        detached create_task with no stored handle, so it can't be cancelled here; it
        is short-lived and self-terminates."""
        pending: list[asyncio.Task] = []
        for run in list(self.runs.values()):
            for t in (run.task, run.standby_task):
                if t is not None and not t.done():
                    t.cancel()
                    pending.append(t)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        managers: dict[int, Any] = {}
        for run in list(self.runs.values()):
            if run.pool_manager is not None:
                managers[id(run.pool_manager)] = run.pool_manager
        for manager in managers.values():
            try:
                await manager.close()
            except Exception:
                LOG.warning("runtime pool close failed during manager shutdown", exc_info=True)
