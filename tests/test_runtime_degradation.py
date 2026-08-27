"""Deterministic coverage for dswarm/swarm/runtime_degradation.py.

This mixin previously had zero direct tests while being wired into Swarm via
multiple inheritance (dswarm/swarm/swarm.py). These tests pin its observable
contract with a minimal host object instead of a full Swarm:

- degrade/recover emits exactly one BLACKBOARD_DELTA per transition, deduped
  by identical reason, re-emitted when the reason changes;
- a missing event bus or a raising bus.emit never disturbs the caller;
- sync contexts without a running loop degrade to silence (state still set);
- _runtime_metadata_for flips backend to "local" while degradation is recorded;
- _record_runtime_degraded appends a bounded payload and schedules one delta.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dswarm.core.events import EventType
from dswarm.swarm.runtime_degradation import RuntimeDegradationMixin


class _FakeBus:
    """Records emitted events; can be made to raise like a wedged sink."""

    def __init__(self) -> None:
        self.events: list = []
        self.boom = False

    async def emit(self, event) -> object:
        if self.boom:
            raise RuntimeError("bus wedged")
        self.events.append(event)
        return event


class _Host(RuntimeDegradationMixin):
    """Minimal mixin host mirroring the attributes the real Swarm supplies."""

    def __init__(self, *, bus: _FakeBus | None = None,
                 worker_backend: str = "container") -> None:
        self._degraded_engines: dict[str, str] = {}
        self._runtime_degraded: list[dict] = []
        self.bus = bus if bus is not None else _FakeBus()
        self.run_id = "run-deg"
        self.challenge = SimpleNamespace(id="ch-deg")
        self.worker_backend = worker_backend

    def _runtime_for_engine(self, engine: str, profile) -> dict:
        return {"id": f"rt-{engine}"} if engine else {}

    def _profile_for_engine(self, engine: str, *, advance: bool) -> dict | None:
        return {"name": f"prof-{engine}"} if engine else None


def _kinds(host: _Host) -> list[str]:
    kinds = []
    for ev in host.bus.events:
        payload = getattr(ev, "payload", {}) or {}
        kinds.append(payload.get("kind", ""))
    return kinds


async def _settle() -> None:
    # let every fire-and-forget create_task from sync note methods run
    for _ in range(3):
        await asyncio.sleep(0)


async def test_degrade_recover_sequence_and_dedupe():
    h = _Host()

    h._note_engine_degraded("pi", "probe failed", role="worker")
    await _settle()
    assert h._degraded_engines == {"pi": "probe failed"}
    assert _kinds(h) == ["engine_degraded"]
    first = h.bus.events[0]
    assert getattr(first, "event_type") is EventType.BLACKBOARD_DELTA
    body = dict(first.payload)
    assert body["kind"] == "engine_degraded"
    assert body["status"] == "degraded" and body["engine"] == "pi"
    assert body["actor"] == "coordinator"

    # same engine + identical reason → deduped, no second delta
    h._note_engine_degraded("pi", "probe failed", role="worker")
    await _settle()
    assert _kinds(h) == ["engine_degraded"]

    # changed reason → new transition emitted
    h._note_engine_degraded("pi", "probe failed harder", role="worker")
    await _settle()
    assert _kinds(h) == ["engine_degraded", "engine_degraded"]

    # recovery pops state and emits a recovered payload
    h._note_engine_recovered("pi")
    await _settle()
    assert h._degraded_engines == {}
    assert _kinds(h)[-1] == "engine_degraded"  # same channel, different status
    last = dict(h.bus.events[-1].payload)
    assert last["status"] == "recovered" and last["reason"] == ""

    # recovering an unknown/healthy engine is a silent no-op
    h._note_engine_recovered("pi")
    await _settle()
    assert len(h.bus.events) == 3


async def test_missing_bus_and_raising_emit_never_disturb_caller():
    h = _Host(bus=None)
    h._note_engine_degraded("pi", "x", role="worker")
    await _settle()                    # emit early-returns; nothing raised
    assert h._degraded_engines == {"pi": "x"}

    boom_host = _Host(bus=_FakeBus())
    boom_host.bus.boom = True
    boom_host._note_engine_degraded("pi", "y", role="worker")
    await _settle()                    # swallowed by the mixin's except path
    assert boom_host._degraded_engines == {"pi": "y"}
    assert boom_host.bus.events == []

    # awaited directly it also stays quiet
    await boom_host._emit_runtime_degraded({"engine": "pi"})


def test_no_running_loop_degrades_to_silence_but_keeps_state():
    h = _Host()  # called OUTSIDE any event loop
    h._note_engine_degraded("pi", "offline note", role="worker")
    h._note_engine_recovered("pi")
    assert h._degraded_engines == {}   # recover popped the degraded entry
    h2 = _Host()
    h2._note_engine_degraded("pi", "stays", role="worker")
    assert h2._degraded_engines == {"pi": "stays"}


async def test_runtime_metadata_flips_backend_while_degraded():
    h = _Host(worker_backend="container")

    meta = h._runtime_metadata_for()
    assert meta == {"backend": "container", "runtime": "", "runtime_degraded": []}

    h._record_runtime_degraded(
        engine="pi", profile={"name": "prof-pi"},
        reason="docker unavailable" * 60,          # exercises the [:300] bound
        requested_backend="container",
    )
    meta = h._runtime_metadata_for()
    assert meta["backend"] == "local"
    # without an outcome there is no engine context, so runtime stays empty even
    # while a degradation is recorded
    assert meta["runtime"] == ""
    assert len(meta["runtime_degraded"]) == 1
    entry = meta["runtime_degraded"][0]
    assert entry["engine"] == "pi" and entry["backend"] == "local"
    assert entry["requested_backend"] == "container"
    assert len(entry["reason"]) <= 300 and "docker unavailable" in entry["reason"]

    # returned list is a copy: mutating it cannot corrupt internal state
    meta["runtime_degraded"].append({"engine": "ghost"})
    assert len(h._runtime_degraded) == 1

    await _settle()
    assert _kinds(h) == ["runtime_degraded"]       # scheduled exactly once

    # an outcome supplies the engine context, which resolves profile + runtime ids
    resolved = h._runtime_metadata_for(SimpleNamespace(engine="pi"))
    assert resolved["runtime"] == "rt-pi"
    assert resolved["backend"] == "local"


@pytest.mark.parametrize("outcome_engine", ["", None])
async def test_metadata_without_outcome_has_empty_runtime(outcome_engine):
    h = _Host(worker_backend="container")
    outcome = (
        SimpleNamespace(engine=outcome_engine) if outcome_engine else None)
    meta = h._runtime_metadata_for(outcome)
    assert meta["runtime"] == ""


# helper so the sync test above can create/clear a loop explicitly if needed
async def _noop() -> None:
    return None