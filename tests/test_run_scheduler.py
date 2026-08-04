"""P4 run scheduler — FIFO queue + global concurrency cap (route A, P4).

Covers the BTFly-ported policy (limit clamp, queued position, FIFO dispatch,
queued cancel/hold) at three levels: the pure RunScheduler policy, the
RunManager integration (events + lifecycle), and the HTTP API.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from apps.web.run_manager import RunManager
from apps.web.run_scheduler import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    MAX_CONCURRENT_RUNS,
    MIN_CONCURRENT_RUNS,
    RunScheduler,
)
from apps.web.server import create_app
from muteki.core.events import Event, EventType


# ── helpers ──────────────────────────────────────────────────────────────────

def _scheduler(tmp_path: Path) -> RunScheduler:
    return RunScheduler(sessions_root=str(tmp_path / "sessions"))


def _quick_driver(emit_started: bool = True, delay: float = 0.05):
    """A driver that starts, idles briefly, and finishes — a real run shape."""
    async def driver(run) -> None:
        if emit_started:
            await run.bus.emit(Event(
                event_type=EventType.RUN_STARTED, run_id=run.run_id,
                payload={"challenge": {"id": run.run_id}}))
        await asyncio.sleep(delay)
        await run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED, run_id=run.run_id,
            payload={"solved": False, "flags": [],
                     "expected_flags": 1, "multi_flag": False}))
    return driver


def _manager(tmp_path: Path, *, limit: int = DEFAULT_MAX_CONCURRENT_RUNS) -> RunManager:
    mgr = RunManager(sessions_root=str(tmp_path / "sessions"))
    mgr.set_scheduler_limit(limit)
    return mgr


async def _wait_done(run, timeout: float = 5.0) -> None:
    if run.task is not None:
        await asyncio.wait_for(run.task, timeout=timeout)


def _events(run) -> list:
    seen: list = []

    def _sink(ev) -> None:
        seen.append(ev.event_type)
    run.bus.add_sink(_sink)
    return seen


# ── RunScheduler policy ──────────────────────────────────────────────────────

def test_submit_below_limit_dispatches_immediately(tmp_path):
    s = _scheduler(tmp_path)
    assert s.submit("r1", object()) is None  # slot free → active right away
    assert s.is_active("r1")
    assert not s.is_queued("r1")
    assert s.active_count == 1


def test_submit_at_limit_queues_fifo_with_positions(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(2)
    assert s.submit("r1", object()) is None
    assert s.submit("r2", object()) is None
    assert s.submit("r3", object()) == 1   # first in queue
    assert s.submit("r4", object()) == 2
    assert s.queued_count == 2
    assert s.position("r3") == 1
    assert s.position("r4") == 2
    # idempotent: re-submitting a queued run returns its existing position
    assert s.submit("r3", object()) == 1


def test_next_to_dispatch_is_fifo_and_marks_active(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(1)
    s.submit("r1", object())            # immediate (active)
    s.submit("r2", "d2")
    s.submit("r3", "d3")
    rid, driver = s.next_to_dispatch()
    assert (rid, driver) == ("r2", "d2")
    assert s.is_active("r2")
    rid, driver = s.next_to_dispatch()
    assert (rid, driver) == ("r3", "d3")
    assert s.next_to_dispatch() is None


def test_release_frees_slot(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(1)
    s.submit("r1", object())
    s.submit("r2", "d2")
    s.release("r1")
    rid, _ = s.next_to_dispatch()
    assert rid == "r2"


def test_cancel_queued_removes_and_shifts_positions(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(1)
    s.submit("r1", object())
    s.submit("r2", "d2")
    s.submit("r3", "d3")
    assert s.cancel("r2") is True
    assert not s.is_queued("r2")
    assert s.position("r3") == 1        # shifted forward
    assert s.cancel("r2") is False      # already gone
    assert s.cancel("r1") is False      # active runs aren't queue-cancelled


def test_pause_hold_skips_but_keeps_position(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(1)
    s.submit("r1", object())
    s.submit("r2", "d2")
    s.submit("r3", "d3")
    assert s.pause("r2") is True
    assert s.is_held("r2")
    rid, _ = s.next_to_dispatch()       # r2 held → r3 dispatches first
    assert rid == "r3"
    assert s.position("r2") == 1        # still queued, still first
    assert s.resume("r2") is True
    rid, _ = s.next_to_dispatch()
    assert rid == "r2"
    # pause only applies to queued runs
    assert s.pause("r1") is False
    assert s.pause("gone") is False


def test_set_limit_clamps_and_persists(tmp_path):
    s = _scheduler(tmp_path)
    assert s.set_limit(0) == MIN_CONCURRENT_RUNS
    assert s.set_limit(999) == MAX_CONCURRENT_RUNS
    assert s.set_limit(3) == 3
    # persisted across instances
    s2 = RunScheduler(sessions_root=str(tmp_path / "sessions"))
    assert s2.max_concurrent_runs == 3


def test_env_limit_seed(monkeypatch, tmp_path):
    monkeypatch.setenv("MUTEKI_MAX_CONCURRENT_RUNS", "2")
    assert RunScheduler(str(tmp_path / "s")).max_concurrent_runs == 2
    monkeypatch.setenv("MUTEKI_MAX_CONCURRENT_RUNS", "99")
    assert RunScheduler(str(tmp_path / "s")).max_concurrent_runs == MAX_CONCURRENT_RUNS
    monkeypatch.delenv("MUTEKI_MAX_CONCURRENT_RUNS")
    assert RunScheduler(str(tmp_path / "s")).max_concurrent_runs == DEFAULT_MAX_CONCURRENT_RUNS


def test_snapshot_shape(tmp_path):
    s = _scheduler(tmp_path)
    s.set_limit(1)
    s.submit("r1", object())
    s.submit("r2", "d2")
    snap = s.snapshot()
    assert snap["settings"] == {"max_concurrent_runs": 1}
    assert snap["active_count"] == 1
    assert snap["queued_count"] == 1
    assert snap["queue"][0]["run_id"] == "r2"
    assert snap["queue"][0]["position"] == 1
    assert snap["queue"][0]["queued_at"] > 0


# ── RunManager integration ───────────────────────────────────────────────────

def test_manager_start_immediate_below_limit(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=5)
        run = await mgr.start("r1", _quick_driver())
        assert run.queued is False
        assert run.queue_position is None
        assert run.status() == "running"   # task pending, not yet finished
        await _wait_done(run)
        assert run.status() == "finished"
    asyncio.run(go())


def test_manager_start_queues_at_limit_and_emits_events(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        a = await mgr.start("a", _quick_driver())
        b = mgr.create("b")                     # same handle start() will use
        evs = _events(b)                        # attach the sink BEFORE dispatch
        await mgr.start("b", _quick_driver())
        assert b.queued is True
        assert b.queue_position == 1
        assert b.status() == "queued"
        assert EventType.RUN_QUEUED in evs
        # when the active run finishes, the queued one dispatches
        await _wait_done(a)
        assert b.queued is False
        assert b.queue_position is None
        assert b.status() == "running"
        await _wait_done(b)
    asyncio.run(go())


def test_manager_fifo_dispatch_order(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        order: list[str] = []

        def make_driver(name: str):
            async def driver(run) -> None:
                order.append(name)
                await asyncio.sleep(0.02)
                await run.bus.emit(Event(
                    event_type=EventType.RUN_FINISHED, run_id=run.run_id,
                    payload={"solved": False, "flags": []}))
            return driver

        a = await mgr.start("a", make_driver("A"))
        b = await mgr.start("b", make_driver("B"))
        c = await mgr.start("c", make_driver("C"))
        assert (b.queue_position, c.queue_position) == (1, 2)
        await _wait_done(a)
        await _wait_done(b)
        await _wait_done(c)
        return order
    assert asyncio.run(go()) == ["A", "B", "C"]


def test_manager_cancel_queued_settles_run(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        a = await mgr.start("a", _quick_driver())
        b = await mgr.start("b", _quick_driver())
        evs = _events(b)
        assert await mgr.cancel_run("b") is True
        assert b.queued is False
        assert b.cancelled is True
        assert b.status() == "cancelled"
        assert EventType.RUN_CANCELLED in evs
        assert EventType.RUN_FINISHED in evs
        # queue entry gone; the next submit starts at position 1 again
        c = await mgr.start("c", _quick_driver())
        assert c.queued is True and c.queue_position == 1
        await _wait_done(a)
        await _wait_done(c)
    asyncio.run(go())


def test_manager_pause_resume_queued_hold(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        a = await mgr.start("a", _quick_driver())
        b = await mgr.start("b", _quick_driver())
        c = await mgr.start("c", _quick_driver())
        # hold b: c must dispatch when the slot frees
        assert await mgr.pause_queued("b") is True
        assert b.status() == "paused"
        await _wait_done(a)
        assert c.queued is False       # c jumped the held b
        assert b.queued is True        # b still waiting
        await _wait_done(c)
        # resume b → it dispatches now that a slot is free
        assert await mgr.resume_queued("b") is True
        assert b.queued is False
        await _wait_done(b)
    asyncio.run(go())


def test_manager_limit_raise_dispatches(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        a = await mgr.start("a", _quick_driver(delay=0.2))
        b = await mgr.start("b", _quick_driver())
        assert b.queued is True
        assert mgr.set_scheduler_limit(2) == 2
        await mgr.dispatch_pending()
        assert b.queued is False       # raised limit → dispatched immediately
        await _wait_done(a)
        await _wait_done(b)
    asyncio.run(go())


def test_manager_no_double_dispatch_same_run(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=5)
        run = await mgr.start("dup", _quick_driver(delay=0.2))
        again = await mgr.start("dup", _quick_driver())
        assert again is run            # same handle returned, not re-launched
        task = run.task
        await _wait_done(run)
        assert run.task is task        # still the ONE task
    asyncio.run(go())


def test_manager_scheduler_snapshot_enriched(tmp_path):
    async def go():
        mgr = _manager(tmp_path, limit=1)
        a = await mgr.start("a", _quick_driver())
        b = await mgr.start("b", _quick_driver())
        b.name = "second challenge"
        b.category = "crypto"
        snap = mgr.scheduler_snapshot()
        assert snap["active_count"] == 1
        assert snap["queued_count"] == 1
        assert snap["queue"][0]["run_id"] == "b"
        assert snap["queue"][0]["title"] == "second challenge"
        assert snap["queue"][0]["category"] == "crypto"
        assert snap["queue"][0]["position"] == 1
        await _wait_done(a)
        await _wait_done(b)
    asyncio.run(go())


# ── HTTP API ─────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Server:
    def __init__(self, app) -> None:
        self.port = _free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def api_server(tmp_path):
    app = create_app(RunManager(sessions_root=str(tmp_path / "sessions")))
    with _Server(app) as s:
        yield s


def test_api_scheduler_get_put(api_server):
    r = httpx.get(f"{api_server.base}/api/scheduler")
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["max_concurrent_runs"] == 5
    assert body["active_count"] == 0
    assert body["queued_count"] == 0
    assert body["queue"] == []

    r = httpx.put(f"{api_server.base}/api/scheduler",
                  json={"max_concurrent_runs": 1})
    assert r.status_code == 200
    assert r.json()["settings"]["max_concurrent_runs"] == 1

    # clamp: out-of-range values snap to [1, 8]
    r = httpx.put(f"{api_server.base}/api/scheduler",
                  json={"max_concurrent_runs": 99})
    assert r.json()["settings"]["max_concurrent_runs"] == 8
    r = httpx.put(f"{api_server.base}/api/scheduler",
                  json={"max_concurrent_runs": 0})
    assert r.json()["settings"]["max_concurrent_runs"] == 1
    r = httpx.put(f"{api_server.base}/api/scheduler",
                  json={"max_concurrent_runs": "nope"})
    assert r.status_code == 400


def test_api_start_queues_and_patch_cancel(api_server):
    # cap at 1; the first (idle) run holds the slot so the second must queue
    assert httpx.put(f"{api_server.base}/api/scheduler",
                     json={"max_concurrent_runs": 1}).status_code == 200
    r1 = httpx.post(f"{api_server.base}/api/runs/queue-a/start", json={"kind": "idle"})
    assert r1.status_code == 200
    assert "queued" not in r1.json()          # immediate dispatch, no queue marker
    r2 = httpx.post(f"{api_server.base}/api/runs/queue-b/start", json={"kind": "idle"})
    assert r2.status_code == 200
    assert r2.json()["queued"] is True
    assert r2.json()["position"] == 1

    # the queued run shows up in the scheduler snapshot
    snap = httpx.get(f"{api_server.base}/api/scheduler").json()
    assert snap["queued_count"] == 1
    assert snap["queue"][0]["run_id"] == "queue-b"
    assert snap["queue"][0]["title"] == "queue-b"   # falls back to the run id

    # PATCH cancel drops it from the queue and settles it
    r = httpx.patch(f"{api_server.base}/api/runs/queue-b", json={"cancel": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["run"]["status"] == "cancelled"
    snap = httpx.get(f"{api_server.base}/api/scheduler").json()
    assert snap["queued_count"] == 0

    # a queued run can also be paused (held) and resumed
    r3 = httpx.post(f"{api_server.base}/api/runs/queue-c/start", json={"kind": "idle"})
    assert r3.json()["queued"] is True
    assert httpx.patch(f"{api_server.base}/api/runs/queue-c",
                       json={"pause": True}).json()["run"]["status"] == "paused"
    assert httpx.patch(f"{api_server.base}/api/runs/queue-c",
                       json={"resume": True}).json()["run"]["status"] == "queued"
