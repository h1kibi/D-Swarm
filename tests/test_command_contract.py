from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_post_hitl_normalizes_prompt_and_expands_recipients(tmp_path):
    from apps.web import run_manager as rm

    manager = rm.RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("run-command")
    queued = []
    async def queue_put(item):
        queued.append(item)
    run.hitl.put = queue_put
    emitted = []
    async def bus_emit(item):
        emitted.append(item)
    run.bus.emit = bus_emit
    run.task = SimpleNamespace(done=lambda: False)

    assert await manager.post_hitl(
        "run-command", "solver:a,solver:b", "prompt", text="inspect /admin"
    )
    assert [item["target"] for item in queued] == ["solver:a", "solver:b"]
    assert all(item["action"] == "hint" for item in queued)
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_post_hitl_review_is_rejected_for_live_run(tmp_path):
    from apps.web import run_manager as rm

    manager = rm.RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("run-live-review")
    run.task = SimpleNamespace(done=lambda: False)
    queued = []
    async def queue_put(item):
        queued.append(item)
    run.hitl.put = queue_put

    assert await manager.post_hitl("run-live-review", "global", "review") is False
    assert queued == []


@pytest.mark.asyncio
async def test_post_hitl_review_is_read_only_for_terminal_run(tmp_path, monkeypatch):
    from apps.web import run_manager as rm

    manager = rm.RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("run-ended-review")
    run.finished = True
    run.task = None
    emitted = []

    async def bus_emit(item):
        emitted.append(item)

    run.bus.emit = bus_emit
    monkeypatch.setattr(manager, "_ensure_standby", lambda *_: (_ for _ in ()).throw(AssertionError("review must not start standby")))

    assert await manager.post_hitl("run-ended-review", "global", "review") is True
    assert emitted == []


@pytest.mark.asyncio
async def test_post_hitl_resume_reopens_terminal_run_through_reason(tmp_path, monkeypatch):
    from apps.web import run_manager as rm

    manager = rm.RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("run-ended-resume")
    run.finished = True
    run.started = True
    calls = []
    async def fake_resolve(run_id, body):
        calls.append((run_id, body))
        return True

    monkeypatch.setattr(manager, "resolve", fake_resolve)
    assert await manager.post_hitl("run-ended-resume", "reason", "resume") is True
    assert calls == [("run-ended-resume", {"operator_resume": True})]
