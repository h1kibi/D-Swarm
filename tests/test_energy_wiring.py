"""M7-0 scheduler wiring tests (docs/10 items 30, 62, 74, 103, 125)."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm import energy, energy_capture
from dswarm.swarm.energy_sidecar import EnergyTraceSink
from dswarm.swarm.board import MemoryBoard
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.solver.reason import Intent, ReasonResult


def _challenge(**overrides) -> Challenge:
    values = {
        "id": "c-reason", "name": "reason-test", "category": "web",
        "points": 50, "description": "solve me",
        "flag_format": r"flag\{[^}]+\}", "target": "https://example.test/",
    }
    values.update(overrides)
    return Challenge(**values)


def _outcome(flag=None) -> SimpleNamespace:
    return SimpleNamespace(flag=flag, flags=[flag] if flag else [],
                           engine="pi-worker")


def _one_intent_reason():
    seen = {"n": 0}

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        if seen["n"] == 0:
            seen["n"] += 1
            return ReasonResult(
                goal_met=False,
                intents=[Intent(intent_id="intent-1", goal="exploit it",
                                mode="explore", direction="web",
                                priority=0.5)],
                audit_notes=[])
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    return reason_fn


async def _run_one_cycle(**kw) -> list[SimpleNamespace]:
    board = MemoryBoard("c-reason")
    stop = asyncio.Event()
    calls: list[SimpleNamespace] = []

    async def worker(decision, profile):
        calls.append(decision)
        if decision.mode != "recon":
            stop.set()
        return _outcome()

    swarm = ReasonSwarm(
        _challenge(), board=board, worker_factory=worker,
        reason_fn=_one_intent_reason(), stop_event=stop, **kw)
    await swarm.run()
    return calls


# ---------------------------------------------------------------- 30/103

def test_30_and_103_feature_off_zero_capture_dispatch_equivalent(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        energy_capture, "capture_energy_cycle_snapshot",
        lambda *a, **k: captured.append(a))
    calls = asyncio.run(_run_one_cycle(energy_trace_enabled=False))
    assert captured == []  # capture never invoked
    assert [c.mode for c in calls] == ["recon", "explore"]
    assert calls[1].intent_id == "intent-1"  # dispatch per-decision equivalent


def test_103_enabled_without_sink_is_construction_error():
    with pytest.raises(ValueError):
        ReasonSwarm(_challenge(), energy_trace_enabled=True,
                    energy_trace_sink=None)


# ------------------------------------------------------------------ 62/74

class _SpySink:
    """Records two-phase order against _register_decision."""

    def __init__(self, order: list[str], traces: list):
        self._order = order
        self._traces = traces

    def start_cycle(self, trace_id, *, reason_cycle_id, decision_ts):
        self._order.append("started")
        return True

    def write_trace(self, trace):
        self._order.append("traced")
        self._traces.append(trace)
        return True


def test_62_and_74_two_phase_before_register_and_field_ownership(monkeypatch):
    order: list[str] = []
    traces: list = []
    spy = _SpySink(order, traces)

    real_register = ReasonSwarm._register_decision

    def register(self, decision):
        order.append("register")
        return real_register(self, decision)

    monkeypatch.setattr(ReasonSwarm, "_register_decision", register)
    calls = asyncio.run(_run_one_cycle(energy_trace_enabled=True,
                                       energy_trace_sink=spy))
    assert calls[1].intent_id == "intent-1"
    # two-phase protocol strictly before registration/dispatch of the fresh
    # decision (the first "register" is the recon worker, which precedes the
    # reason cycle).
    assert order == ["register", "started", "traced", "register"]
    trace = traces[0]
    assert trace.expected_decision_count == 1
    decision = trace.decisions[0]
    assert decision.intent_id == "intent-1"
    assert decision.original_index == 0
    assert decision.worker_lane == "ordinary"
    assert decision.priority_scale == "planner"
    assert decision.decision_source == "reason"
    assert decision.normalized_priority == 0.5
    assert energy.decision_id_matches(decision, "c-reason")


def test_62_real_sink_writes_cycle_started_before_trace(tmp_path):
    sink = EnergyTraceSink(tmp_path / "run", run_id="c-reason",
                           challenge_id="c-reason", enabled=True)
    asyncio.run(_run_one_cycle(energy_trace_enabled=True,
                               energy_trace_sink=sink))
    segments = sorted((tmp_path / "run" / "metrics").glob(
        "energy-cycle-traces.*.jsonl"))
    lines = []
    for seg in segments:
        for raw in seg.read_bytes().split(b"\n"):
            if raw.strip():
                lines.append(json.loads(raw.decode("utf-8")))
    assert [r["kind"] for r in lines] == ["cycle_started", "cycle_trace"]
    trace = lines[1]
    assert trace["decisions"][0]["original_index"] == 0
    assert trace["decisions"][0]["priority_scale"] == "planner"
    # no graph in this harness: capture degrades to an incomplete snapshot,
    # and the trace carries the exclusion honestly (docs/10 excluded stub).
    assert trace["complete"] is False
    assert trace["exclusion_reason"] == "snapshot_unavailable"


# ------------------------------------------------------------------ 125/85

def test_125_sink_only_reachable_from_record_path():
    """Live HITL pause/resume never touches the sink: the only call sites are
    construction, the gating condition, and _record_energy_cycle."""
    import dswarm.swarm.reason_scheduler as rs
    src = inspect.getsource(rs)
    # 8 occurrences: constructor param, assignment (x2 on one line), the
    # enabled-without-sink validation, its error message, the gating condition,
    # and the two _record_energy_cycle calls. Nothing else (no pause path).
    assert src.count("energy_trace_sink") == 8
    record_src = inspect.getsource(ReasonSwarm._record_energy_cycle)
    assert "pause" not in record_src
    # energy modules stay bus-free (docs/10: sidecar-only, no EventBus)
    energy_src = inspect.getsource(energy)
    assert "EventType" not in energy_src
    capture_src = inspect.getsource(energy_capture)
    assert "EventType" not in capture_src
