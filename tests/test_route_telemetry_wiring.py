"""M6b-2 production telemetry wiring and deterministic replay tests."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

from dswarm.models.solve_graph import Challenge
from dswarm.solver.reason import build_reason_prompt
from dswarm.swarm.board import FindingPredicate, MemoryBoard
from dswarm.swarm.projection import BoardProjector
from dswarm.swarm.route_replay import ReplayClock, replay_route_observations
from dswarm.swarm.route_telemetry import MetricsSink
from dswarm.swarm.shared_graph import (
    IntentRouteRef,
    RouteObservation,
    SQLiteSharedGraph,
)


class RecordingMetricsSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.records = []
        self.fail = fail
        self.delta = {}

    def append(self, record) -> bool:
        if self.fail:
            raise OSError("metrics unavailable")
        self.records.append(record)
        return True

    def aggregate_delta(self):
        if self.fail:
            raise OSError("metrics unavailable")
        delta, self.delta = self.delta, {}
        return delta


class CaptureBus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


def challenge() -> Challenge:
    return Challenge(id="m6-wiring", name="M6 wiring", category="web")


def open_graph(tmp_path, sink=None, *, name="graph.db") -> SQLiteSharedGraph:
    return SQLiteSharedGraph.open(
        db_path=tmp_path / name,
        challenge=challenge(),
        metrics_sink=sink,
    )


def test_graph_records_append_dedupe_and_first_summary_without_graph_events(tmp_path):
    sink = RecordingMetricsSink()
    graph = open_graph(tmp_path, sink)

    fact_seq = graph.add_evidence(
        actor="cli-web",
        source="curl",
        fact="login accepts a quote",
        verified=False,
        route_hash="SQL Injection",
    )
    event_count_after_append = len(graph.events_since(0))
    assert graph.add_evidence(
        actor="cli-web",
        source="curl",
        fact="login accepts a quote",
        verified=False,
        route_hash="SQL Injection",
    ) == -1
    assert graph.record_fact_summary(fact_seq=fact_seq, summary="登录点疑似 SQL 注入") is True
    assert graph.record_fact_summary(fact_seq=fact_seq, summary="登录点疑似 SQL 注入") is True

    assert [record.kind for record in sink.records] == [
        "fact_appended",
        "dedupe_hit",
        "summary_recorded",
    ]
    assert sink.records[0].fact_seq == fact_seq
    assert sink.records[0].route_hash == "sqli"
    assert sink.records[0].route_lineage == "explicit"
    assert sink.records[1].record_id.startswith("dedupe:")
    assert sink.records[1].record_id.endswith(f":fact:{fact_seq}")
    assert len(graph.events_since(0)) == event_count_after_append + 1
    assert [event["kind"] for event in graph.events_since(0)] == [
        "fact_added",
        "fact_summarized",
    ]
    graph.close()


def test_each_dedupe_collision_is_counted(tmp_path):
    sink = MetricsSink(tmp_path / "workspace", run_id="run-m6-dedupe")
    graph = open_graph(tmp_path, sink, name="dedupe.db")

    graph.add_evidence(
        actor="cli-web", source="curl", fact="same fact", verified=False,
    )
    for _ in range(2):
        assert graph.add_evidence(
            actor="cli-web", source="curl", fact="same fact", verified=False,
        ) == -1

    delta = sink.aggregate_delta()
    assert delta["by_kind"]["dedupe_hit"] == 2
    graph.close()


def test_metrics_failures_never_break_graph_or_projection(tmp_path):
    sink = RecordingMetricsSink(fail=True)
    graph = open_graph(tmp_path, sink)

    fact_seq = graph.add_evidence(
        actor="cli-web", source="curl", fact="health endpoint", verified=True,
    )
    assert fact_seq > 0
    assert graph.record_fact_summary(fact_seq=fact_seq, summary="健康检查端点") is True

    board = MemoryBoard(challenge().id)
    projector = BoardProjector(board, metrics_sink=sink, challenge_id=challenge().id)
    assert projector.sync(graph) == fact_seq
    assert [finding.target for finding in board.query_findings(FindingPredicate())] == [
        "health endpoint"
    ]
    graph.close()


def test_projector_records_base_and_promotion_once(tmp_path):
    sink = RecordingMetricsSink()
    graph = open_graph(tmp_path, sink)
    fact_seq = graph.add_evidence(
        actor="cli-web", source="curl", fact="admin endpoint", verified=False,
        route_hash="web",
    )
    board = MemoryBoard(challenge().id)
    projector = BoardProjector(board, metrics_sink=sink, challenge_id=challenge().id)

    projector.sync(graph)
    projector.sync(graph)
    assert [record.kind for record in sink.records].count("fact_projected") == 1

    assert graph.add_evidence(
        actor="cli-web", source="curl", fact="admin endpoint", verified=True,
        route_hash="web", verifier="curl",
    ) == -1
    projector.sync(graph)
    projector.sync(graph)

    kinds = [record.kind for record in sink.records]
    assert kinds.count("fact_projected") == 1
    assert kinds.count("fact_promoted") == 1
    promoted = next(record for record in sink.records if record.kind == "fact_promoted")
    assert promoted.fact_seq == fact_seq
    assert promoted.verified is True
    graph.close()


def test_metrics_on_and_off_leave_reason_summary_and_graph_events_identical(tmp_path):
    sink = RecordingMetricsSink()
    graph_on = open_graph(tmp_path, sink, name="on.db")
    graph_off = SQLiteSharedGraph.open(
        db_path=tmp_path / "off.db", challenge=challenge()
    )
    for graph in (graph_on, graph_off):
        graph.add_evidence(
            actor="cli-web", source="curl", fact="same fact", verified=True,
            route_hash="web",
        )

    summary_on = graph_on.to_reason_summary()
    summary_off = graph_off.to_reason_summary()
    assert summary_on == summary_off
    assert build_reason_prompt(summary_on) == build_reason_prompt(summary_off)
    strip = lambda graph: [
        {key: value for key, value in event.items() if key != "ts"}
        for event in graph.events_since(0)
    ]
    assert strip(graph_on) == strip(graph_off)
    graph_on.close()
    graph_off.close()


def test_projector_emits_metrics_summary_with_empty_actor_and_no_graph_write():
    async def main() -> None:
        sink = RecordingMetricsSink()
        sink.delta = {
            "records_total": 2,
            "verified_total": 1,
            "by_kind": {"fact_appended": 1, "fact_projected": 1},
            "by_lineage": {"explicit": 2},
            "by_route": {},
        }
        bus = CaptureBus()
        graph = SimpleNamespace(
            events_since=lambda after_seq, kinds=None: [
                {
                    "seq": 1,
                    "ts": 123.0,
                    "actor": "cli-web",
                    "kind": "fact_added",
                    "payload": {"fact": "one fact"},
                    "artifact_id": None,
                    "verified": True,
                    "confidence": 1.0,
                }
            ] if after_seq < 1 else [],
        )
        projector = BoardProjector(
            MemoryBoard("m6-wiring"),
            bus=bus,
            run_id="run-m6",
            challenge_id="m6-wiring",
            metrics_sink=sink,
        )
        projector.sync(graph)
        await asyncio.sleep(0)

        summaries = [
            event for event in bus.events
            if event.payload.get("kind") == "metrics_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0].payload["actor"] == ""
        assert summaries[0].payload["records_total"] >= 2
        assert not hasattr(graph, "add_evidence")

    asyncio.run(main())



def test_swarm_wires_metrics_next_to_graph_workspace(tmp_path):
    from dswarm.swarm.swarm import Swarm

    workspace = tmp_path / "sessions" / "run-m6" / "workspace"
    swarm = Swarm(
        challenge(),
        [],
        llm=None,
        sandbox=SimpleNamespace(root=tmp_path / "sandbox"),
        graph_dir=workspace / "graph",
        run_id="run-m6",
    )

    assert swarm._route_metrics is not None
    assert swarm._route_metrics.path == workspace / "metrics" / "route-telemetry.jsonl"
    assert swarm.shared_graph is not None
    assert swarm.shared_graph._metrics_sink is swarm._route_metrics
    swarm.shared_graph.close()

def test_swarm_graph_survives_metrics_sink_creation_failure(tmp_path, monkeypatch):
    import importlib

    swarm_module = importlib.import_module("dswarm.swarm.swarm")

    def unavailable_metrics(*args, **kwargs):
        raise OSError("metrics directory unavailable")

    monkeypatch.setattr(swarm_module, "MetricsSink", unavailable_metrics)
    workspace = tmp_path / "sessions" / "run-m6-degraded" / "workspace"
    swarm = swarm_module.Swarm(
        challenge(), [], llm=None,
        sandbox=SimpleNamespace(root=tmp_path / "sandbox"),
        graph_dir=workspace / "graph",
        run_id="run-m6-degraded",
    )

    assert swarm._route_metrics is None
    assert swarm.shared_graph is not None
    assert swarm.shared_graph.add_evidence(
        actor="cli-web", source="curl", fact="graph survives metrics failure",
        verified=True,
    ) > 0
    swarm.shared_graph.close()


def _serialize_board(board: MemoryBoard):
    findings = sorted(board.query_findings(FindingPredicate()), key=lambda item: item.seq)
    return [
        {
            "source_seq": item.source_seq,
            "projection_key": item.projection_key,
            "route_hash": item.route_hash,
            "route_lineage": item.route_lineage,
            "event_ts": item.event_ts,
            "projected_at": item.projected_at,
            "pheromone_origin_ts": item.pheromone_origin_ts,
            "created_at": item.created_at,
        }
        for item in findings
    ]


def test_replay_clock_and_route_observation_replay_are_deterministic():
    observations = {
        1: RouteObservation(
            fact_seq=1,
            event_ts=20.0,
            explicit_route_hash="web",
            effective_route_hash="web",
            lineage="explicit",
            reason="explicit_route",
            eligible_for_energy=True,
        ),
        2: RouteObservation(
            fact_seq=2,
            event_ts=10.0,
            inherited_routes=(IntentRouteRef("I-2", "pwn"),),
            effective_route_hash="pwn",
            lineage="inherited",
            reason="intent_product",
            eligible_for_energy=True,
        ),
    }
    events = [
        {"seq": 1, "ts": 20.0, "actor": "a", "kind": "fact_added", "payload": {"fact": "late"}, "verified": True, "confidence": 1.0},
        {"seq": 2, "ts": 10.0, "actor": "b", "kind": "fact_added", "payload": {"fact": "early"}, "verified": True, "confidence": 1.0},
    ]

    class FakeGraph:
        challenge = SimpleNamespace(id="replay-challenge")

        def events_since(self, after_seq, kinds=None):
            return [event for event in events if event["seq"] > after_seq]

        def route_observations(self, fact_seqs):
            return {seq: observations[seq] for seq in fact_seqs}

    clock = ReplayClock()
    clock.set(7.5)
    assert clock.now() == 7.5

    first = replay_route_observations(FakeGraph())
    second = replay_route_observations(FakeGraph())
    first_rows = _serialize_board(first)
    second_rows = _serialize_board(second)

    assert first_rows == second_rows
    assert [row["source_seq"] for row in first_rows] == [2, 1]
    assert [row["projected_at"] for row in first_rows] == [10.0, 20.0]
    assert [row["pheromone_origin_ts"] for row in first_rows] == [10.0, 20.0]
