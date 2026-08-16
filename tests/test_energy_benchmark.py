"""Independent M7 offline benchmark harness tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from dswarm.models.solve_graph import Challenge
from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.board import MemoryBoard
from dswarm.swarm.energy import EnergyConfig, GraphCycleSnapshot
from dswarm.swarm.energy_benchmark import (
    EnergyBenchmarkCase,
    benchmark_result_json,
    run_energy_benchmark,
)
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.swarm.shared_graph import SQLiteSharedGraph


CFG = EnergyConfig({
    "verified_witness": 1.0,
    "verified": 0.8,
    "candidate": 0.5,
})


def _challenge() -> Challenge:
    return Challenge(
        id="bench-challenge",
        name="benchmark",
        category="web",
        points=50,
        description="offline scripted benchmark",
        flag_format=r"flag\{[^}]+\}",
        target="https://example.test/",
    )


def _reason_once():
    calls = 0

    async def reason_fn(_summary: str, _challenge_id: str) -> ReasonResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ReasonResult(
                goal_met=False,
                intents=[Intent(
                    intent_id="intent-1",
                    goal="inspect target",
                    mode="explore",
                    direction="web",
                    priority=0.5,
                )],
                audit_notes=[],
            )
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    return reason_fn


def test_harness_injects_real_reason_swarm_finalizes_and_emits_json(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "dswarm.swarm.energy_capture.capture_energy_cycle_snapshot",
        lambda *_args, **_kwargs: GraphCycleSnapshot(
            graph_after_seq=0,
            observations=(),
            dead_ends=(),
            complete=True,
            exclusion_reason="",
            observed_fact_count=0,
            captured_fact_count=0,
            stored_fact_count=0,
        ),
    )
    stop = asyncio.Event()
    injected = []
    graphs = []

    def factory(sink):
        injected.append(sink)

        async def worker(decision, _profile):
            if decision.mode != "recon":
                stop.set()
            return SimpleNamespace(flag=None, flags=[], engine="pi-worker")

        graph = SQLiteSharedGraph.open(
            db_path=tmp_path / "bench-graph.db", challenge=_challenge(),
        )
        graphs.append(graph)
        swarm = ReasonSwarm(
            _challenge(),
            board=MemoryBoard("bench-challenge"),
            worker_factory=worker,
            reason_fn=_reason_once(),
            stop_event=stop,
            run_id="bench-ok",
            graph=graph,
            energy_trace_enabled=True,
            energy_trace_sink=sink,
        )
        assert swarm.energy_trace_sink is sink
        return swarm

    run_root = tmp_path / "bench-ok"
    result = asyncio.run(run_energy_benchmark([
        EnergyBenchmarkCase(
            run_id="bench-ok",
            challenge_id="bench-challenge",
            run_root=run_root,
            swarm_factory=factory,
        )
    ], config=CFG))

    assert len(injected) == 1
    graphs[0].close()
    assert result.cases[0].execution_status == "completed"
    assert result.cases[0].finalized is True
    assert result.cases[0].estimate.dataset_status == "complete"
    manifest = json.loads((
        run_root / "metrics" / "energy-cycle-traces.manifest.json"
    ).read_text(encoding="utf-8"))
    assert manifest["lifecycle_status"] == "finalized"
    payload = json.loads(benchmark_result_json(result))
    assert payload["kind"] == "m7_offline_scheduling_reorder_estimate"
    assert payload["cases"][0]["run_id"] == "bench-ok"
    assert payload["report"]["qualified_runs"] == 1


def test_harness_finalizes_after_case_error_and_excludes_it_from_report(tmp_path):
    class BrokenSwarm:
        async def run(self):
            raise RuntimeError("scripted benchmark failure")

    run_root = tmp_path / "bench-error"
    result = asyncio.run(run_energy_benchmark([
        EnergyBenchmarkCase(
            run_id="bench-error",
            challenge_id="bench-challenge",
            run_root=run_root,
            swarm_factory=lambda _sink: BrokenSwarm(),
        )
    ], config=CFG))

    case = result.cases[0]
    assert case.execution_status == "error"
    assert case.error == "RuntimeError: scripted benchmark failure"
    assert case.finalized is True
    assert case.estimate.dataset_status == "incomplete"
    assert "execution_error" in case.estimate.exclusion_reasons
    assert result.report.qualified_runs == 0
    manifest = json.loads((
        run_root / "metrics" / "energy-cycle-traces.manifest.json"
    ).read_text(encoding="utf-8"))
    assert manifest["lifecycle_status"] == "finalized"
