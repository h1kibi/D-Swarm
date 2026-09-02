"""Durable event log 鈥?append every event to JSONL (one file per run), replay later.

This is what makes "replay any challenge's full solve after the match" work.
It registers as a sink on the EventBus so persistence is automatic.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import AsyncIterator

from pydantic import ValidationError

from dswarm.core.events import Event
from dswarm.core.storage import safe_run_storage_key


_REPLAY_YIELD_EVERY = 100
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _path_lock(path: Path) -> threading.Lock:
    """Serialize appends from multiple SessionStore instances in one process."""
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


class SessionStore:
    def __init__(self, root: str | Path = "sessions") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, run_id: str) -> Path:
        safe = safe_run_storage_key(run_id)
        return self.root / f"{safe}.jsonl"

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        key = safe_run_storage_key(run_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _iter_events(path: Path):
        """Yield valid events, tolerating only a torn final JSONL record.

        A process can terminate after writing part of the final line.  The
        durable prefix remains useful and should still be replayable.  A
        newline-terminated malformed record is different: it indicates
        corruption and must remain visible to callers instead of being silently
        discarded.
        """
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    yield Event.model_validate_json(raw)
                except (json.JSONDecodeError, ValidationError):
                    if not raw.endswith(("\n", "\r")):
                        continue
                    raise

    async def append(self, event: Event) -> None:
        path = self._path(event.run_id)
        line = event.model_dump_json() + "\n"
        async with self._lock_for(event.run_id):
            # Synchronous append under an async lock; writes are small and the
            # OS buffers them. Keeps ordering per run without a thread pool.
            with _path_lock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)

    async def append_checked(self, event: Event) -> None:
        """Append an event and force it to durable storage before returning."""
        path = self._path(event.run_id)
        line = event.model_dump_json() + "\n"
        async with self._lock_for(event.run_id):
            with _path_lock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())

    # EventBus sink signature
    async def sink(self, event: Event) -> None:
        await self.append(event)

    def read_events(self, run_id: str) -> list[Event]:
        """Synchronously read a run event log for startup projection rebuilds."""
        path = self._path(run_id)
        if not path.exists():
            return []
        return list(self._iter_events(path))
    async def replay(self, run_id: str) -> AsyncIterator[Event]:
        path = self._path(run_id)
        if not path.exists():
            return
        n = 0
        for event in self._iter_events(path):
            n += 1
            yield event
            if n % _REPLAY_YIELD_EVERY == 0:
                # Historical SSE subscribers can replay tens of thousands
                # of JSONL events. Cooperate with the uvicorn loop so
                # unrelated API calls do not look globally frozen.
                await asyncio.sleep(0)

    def last_stream_seq(self, run_id: str) -> int:
        """Return the monotonic SSE sequence after normalizing persisted history.

        Old runs can contain a sequence reset after a backend restart/reopen
        (for example 1808, then 1). Raw max(seq) is not enough in that case:
        the browser's Last-Event-ID is a stream cursor, so future buses must
        continue after the normalized cursor, not after the raw max.
        """
        path = self._path(run_id)
        if not path.exists():
            return 0
        seq = 0
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                raw_seq = int(ev.get("seq") or 0)
                seq = max(seq + 1, raw_seq)
        return seq

    async def replay_monotonic(
        self, run_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[Event]:
        """Replay durable history with a strictly increasing stream seq.

        The event payload remains unchanged, but `event.seq` is rewritten for
        transport/reducer identity if a persisted segment reset its raw seq.
        This repairs existing corrupted JSONL without rewriting the file.
        """
        stream_seq = 0
        n = 0
        async for ev in self.replay(run_id):
            raw_seq = int(ev.seq or 0)
            stream_seq = max(stream_seq + 1, raw_seq)
            if stream_seq <= after_seq:
                continue
            n += 1
            if stream_seq != ev.seq:
                ev = ev.model_copy(update={"seq": stream_seq})
            yield ev
            if n % _REPLAY_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    def list_runs(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    def load_all(self, run_id: str) -> list[dict]:
        """Sync convenience for tests / frontends: full event dicts for a run."""
        path = self._path(run_id)
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    out.append(json.loads(raw))
        return out

    def summary(self, run_id: str) -> dict:
        """Cheap one-run digest for the deck's thread rail (name/category/won/flag).

        Scans the persisted JSONL without reconstructing deck state 鈥?pulls the
        challenge identity from run.started and the verdict from run.finished /
        the FlagFound insight. Returns zeros for a run with no events yet.

        Multi-flag aware: carries flags(list)/expected_flags/multi_flag through so a
        rehydrated multi-flag run isn't flattened to a single-flag look-alike. `solved`
        is computed by MODE:
          - single-flag (or mode unknown): a FlagFound is enough to mark solved 鈥?this
            keeps the "ghost run" fallback (FlagFound but no RUN_FINISHED 鈫?still
            shows solved after restart);
          - multi-flag PARTIAL (collected < expected): a FlagFound does NOT mark solved
            (one of three flags is not a win).
        run.finished's explicit `solved` always wins (it knows the real verdict).
        """
        path = self._path(run_id)
        summary = {
            "run_id": run_id, "name": run_id, "category": "",
            "started": False, "finished": False, "solved": False, "flag": None,
            "flags": [], "expected_flags": 1, "multi_flag": False,
            "events": 0, "ts": 0.0,
        }
        if not path.exists():
            return summary

        flags: list[str] = []  # de-duped, order-preserved collected flags

        def _add_flag(val) -> None:
            for f in (val if isinstance(val, list) else [val]):
                if f and f not in flags:
                    flags.append(f)

        finished_solved: bool | None = None  # explicit verdict from run.finished

        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                summary["events"] += 1
                summary["ts"] = ev.get("ts", summary["ts"]) or summary["ts"]
                et = ev.get("event_type")
                p = ev.get("payload") or {}
                if et == "run.started":
                    summary["started"] = True
                    ch = p.get("challenge") or {}
                    summary["name"] = ch.get("name") or summary["name"]
                    summary["category"] = ch.get("category") or summary["category"]
                    if ch.get("expected_flags"):
                        summary["expected_flags"] = int(ch["expected_flags"])
                    if "multi_flag" in ch:
                        summary["multi_flag"] = bool(ch["multi_flag"])
                elif et == "run.titled":
                    # ChatGPT-style auto-title persisted on the run 鈥?survives restart
                    summary["name"] = p.get("title") or summary["name"]
                elif et == "run.finished":
                    summary["finished"] = True
                    finished_solved = bool(p.get("solved"))
                    _add_flag(p.get("flags") or p.get("flag"))
                    # run.finished may carry the authoritative mode (the single-solver
                    # _emit_finished does not 鈥?default fallbacks above cover that).
                    if p.get("expected_flags"):
                        summary["expected_flags"] = int(p["expected_flags"])
                    if "multi_flag" in p:
                        summary["multi_flag"] = bool(p["multi_flag"])
                elif et == "run.reopened":
                    summary["finished"] = False
                    finished_solved = False
                    if p.get("reason") == "resolve":
                        continue
                    bad = p.get("flag")
                    if bad:
                        flags[:] = [f for f in flags if f != bad]
                    else:
                        flags.clear()
                elif et == "insight.event" and p.get("kind") == "FlagFound":
                    _add_flag(p.get("flag"))

        summary["flags"] = flags
        summary["flag"] = flags[0] if flags else None

        # 鈹€鈹€ verdict, by mode 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        if finished_solved is not None:
            summary["solved"] = finished_solved  # explicit verdict wins
        elif flags:
            # no RUN_FINISHED on disk (ghost run) but flags were found. Single-flag /
            # unknown-mode: a found flag is a win. Multi-flag: only a win once the
            # full set is collected (partial 鈮?solved).
            if summary["multi_flag"]:
                summary["solved"] = len(flags) >= summary["expected_flags"]
            else:
                summary["solved"] = True
        return summary

    def summaries(self) -> list[dict]:
        """All persisted runs, newest-activity first 鈥?feeds the rail's Recent."""
        out = [self.summary(rid) for rid in self.list_runs()]
        out.sort(key=lambda s: s.get("ts", 0.0), reverse=True)
        return out
