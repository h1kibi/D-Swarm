"""RunScheduler — FIFO run queue + global concurrency cap (route A, P4).

Ported from BTFly's `internal/agent/service.go` queue semantics (a start that
would exceed the global execution limit lands in a FIFO `queued` wait list;
whenever a slot frees — run end, limit raise, queued cancel — the next queued
run is dispatched), mapped onto dswarm's asyncio + per-run EventBus model.

The scheduler is a PURE POLICY object: it owns the queue, the concurrency
limit, the hold (pause) set, and the persisted settings — but it owns NO event
loops and NO run tasks. RunManager drives it:

  - `submit(run_id, driver)` returns None when a slot is free (the caller
    launches the task immediately) or the 1-based FIFO position otherwise.
    Submitting marks the run ACTIVE synchronously, so concurrent submits can
    never over-subscribe a slot.
  - the manager calls `release(run_id)` from the driver's finally, then
    `next_to_dispatch()` in a loop to fill freed slots FIFO.
  - `pause()`/`resume()` HOLD a queued run: it keeps its queue position but is
    skipped by dispatch until resumed (BTFly has no queued-hold; the plan's
    pause/resume acceptance maps to this + the existing live-run pause).
  - `cancel()` removes a queued run from the queue (a live run's cancel is the
    existing graceful stop, handled by the manager).
  - `set_limit(n)` clamps to [1, 8] (BTFly ExecutionSettings bounds), persists
    to sessions_root/scheduler.json, and a raise triggers immediate dispatch.

All methods are called from the asyncio event loop (single thread) — no locks.

The queue is IN-MEMORY: a server restart drops pending entries (their runs
rehydrate as ghost-finished exactly like any run killed mid-flight). BTFly
persists queued state and re-dispatches on boot; that requires persisting the
driver closure (the /start body) — follow-up.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Optional

DEFAULT_MAX_CONCURRENT_RUNS = 5
MIN_CONCURRENT_RUNS = 1
MAX_CONCURRENT_RUNS = 8


def _env_limit() -> int:
    """Boot-time limit seed: DSWARM_MAX_CONCURRENT_RUNS (clamped); the persisted
    scheduler.json value wins over this once set (set_limit always saves)."""
    raw = (os.environ.get("DSWARM_MAX_CONCURRENT_RUNS") or "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_RUNS
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_RUNS
    return min(MAX_CONCURRENT_RUNS, max(MIN_CONCURRENT_RUNS, n))


class RunScheduler:
    def __init__(self, sessions_root: "str | Path") -> None:
        self._root = Path(sessions_root)
        self._queue: Deque[tuple[str, Any]] = deque()   # (run_id, driver) FIFO
        self._active: set[str] = set()                  # runs holding a slot
        self._held: set[str] = set()                    # paused-while-queued: skipped
        self._queued_at: dict[str, float] = {}          # run_id -> epoch seconds
        self._settings_path = self._root / "scheduler.json"
        self._limit = _env_limit()
        self._load()

    # ── settings persistence ─────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(self._settings_path.read_text(encoding="utf-8"))
            n = int(raw.get("max_concurrent_runs") or 0)
            if n:
                self._limit = min(MAX_CONCURRENT_RUNS, max(MIN_CONCURRENT_RUNS, n))
        except Exception:
            pass  # missing/corrupt → keep env/default seed

    def _save(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps({"max_concurrent_runs": self._limit}, indent=2),
                encoding="utf-8")
        except OSError:
            pass  # best-effort; the runtime limit still applies this session

    @property
    def max_concurrent_runs(self) -> int:
        return self._limit

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    # ── queue operations ─────────────────────────────────────────────────────
    def submit(self, run_id: str, driver: Any) -> "Optional[int]":
        """Register a run for dispatch. Returns None when it can start IMMEDIATELY
        (a slot was free and the run is now ACTIVE — the caller must launch its
        task), or the 1-based FIFO position when it must wait. Idempotent: an
        already-active or already-queued run is not double-submitted."""
        if run_id in self._active:
            return None
        for i, (rid, _) in enumerate(self._queue):
            if rid == run_id:
                return i + 1
        if self.active_count < self._limit:
            self._active.add(run_id)
            return None
        self._queue.append((run_id, driver))
        self._queued_at[run_id] = time.time()
        return len(self._queue)

    def next_to_dispatch(self) -> "Optional[tuple[str, Any]]":
        """Pop the FIRST non-held queued run (FIFO; held runs keep their queue
        position and are skipped) and mark it ACTIVE. None when nothing can
        start. The caller must launch the returned driver's task."""
        for i, (rid, driver) in enumerate(self._queue):
            if rid in self._held:
                continue
            del self._queue[i]
            self._queued_at.pop(rid, None)
            self._active.add(rid)
            return rid, driver
        return None

    def release(self, run_id: str) -> None:
        """Free a run's slot (driver finished/cancelled/failed)."""
        self._active.discard(run_id)
        self._held.discard(run_id)

    def cancel(self, run_id: str) -> bool:
        """Remove a QUEUED run from the queue. False when it wasn't queued (a
        live run's cancel is the manager's graceful stop path)."""
        for i, (rid, _) in enumerate(self._queue):
            if rid == run_id:
                del self._queue[i]
                self._held.discard(run_id)
                self._queued_at.pop(run_id, None)
                return True
        return False

    def pause(self, run_id: str) -> bool:
        """Hold a queued run: skipped by dispatch, keeps its queue position."""
        if not self.is_queued(run_id):
            return False
        self._held.add(run_id)
        return True

    def resume(self, run_id: str) -> bool:
        """Un-hold a queued run so dispatch can pick it up again."""
        was = run_id in self._held
        self._held.discard(run_id)
        return was

    def set_limit(self, n: int) -> int:
        """Clamp + persist the concurrency limit; returns the effective value."""
        self._limit = min(MAX_CONCURRENT_RUNS, max(MIN_CONCURRENT_RUNS, int(n)))
        self._save()
        return self._limit

    # ── queries ──────────────────────────────────────────────────────────────
    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    def is_queued(self, run_id: str) -> bool:
        return any(rid == run_id for rid, _ in self._queue)

    def is_held(self, run_id: str) -> bool:
        return run_id in self._held

    def position(self, run_id: str) -> "Optional[int]":
        for i, (rid, _) in enumerate(self._queue):
            if rid == run_id:
                return i + 1
        return None

    def queued_entries(self) -> "list[tuple[str, int, float]]":
        """(run_id, 1-based position, queued_at) in FIFO order — the scheduler's
        half of the SchedulerStatus snapshot (the manager enriches title/category
        from its run handles)."""
        return [(rid, i + 1, self._queued_at.get(rid, 0.0))
                for i, (rid, _) in enumerate(self._queue)]

    def snapshot(self) -> dict[str, Any]:
        """BTFly SchedulerStatus shape, without per-run title/category (the
        manager's snapshot() enriches those from the live run handles)."""
        return {
            "settings": {"max_concurrent_runs": self._limit},
            "active_count": self.active_count,
            "queued_count": self.queued_count,
            "queue": [
                {"run_id": rid, "position": pos, "queued_at": at}
                for rid, pos, at in self.queued_entries()
            ],
        }
