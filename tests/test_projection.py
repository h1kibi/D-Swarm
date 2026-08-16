"""Structured finding and SharedGraph -> Board projection tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dswarm.core.events import EventType
from dswarm.swarm.board import (
    Finding,
    FindingKind,
    FindingPredicate,
    MemoryBoard,
    PheromoneSettings,
)
from dswarm.swarm.projection import BoardProjector


def _event(seq: int, *, kind: str = "fact_added", payload: dict, verified: bool = True):
    return {
        "seq": seq,
        "actor": "pi-web",
        "kind": kind,
        "payload": payload,
        "artifact_id": "art-1",
        "verified": verified,
        "confidence": 1.0 if verified else 0.4,
    }


def test_structured_finding_project_event_preserves_fields():
    ev = _event(
        7,
        payload={
            "fact": "backend admin panel exposed",
            "finding": {
                "kind": FindingKind.NEW_SURFACE,
                "target": "http://10.0.0.2:8080/admin",
                "data": {"status": 200, "auth": "none"},
                "verified": True,
                "source": "pi-web",
                "artifact_id": "art-1",
                "witness": "curl output",
                "confidence": 1.0,
                "intent_id": "I2",
            },
        },
    )
    finding = BoardProjector.project_event(ev)
    assert finding is not None
    assert finding.kind == FindingKind.NEW_SURFACE
    assert finding.target == "http://10.0.0.2:8080/admin"
    assert finding.payload["status"] == 200
    assert finding.payload["fact"] == "backend admin panel exposed"
    assert finding.source_seq == 7


def test_old_text_fact_projects_to_text_fact():
    ev = _event(8, payload={"fact": "ssh banner reveals OpenSSH 9.2"})
    finding = BoardProjector.project_event(ev)
    assert finding is not None
    assert finding.kind == FindingKind.TEXT_FACT
    assert finding.target == "ssh banner reveals OpenSSH 9.2"


def test_board_projector_sync_is_idempotent_and_incremental():
    board = MemoryBoard("c-project")

    class FakeGraph:
        def __init__(self):
            self.seen = 0

        def events_since(self, after_seq, kinds=None):
            if self.seen == 0:
                self.seen = 1
                return [
                    _event(
                        1,
                        payload={
                            "fact": "login endpoint",
                            "finding": {
                                "kind": FindingKind.HTTP_ENDPOINT,
                                "target": "https://app.test/login",
                                "data": {"status": 200},
                                "verified": True,
                            },
                        },
                    )
                ]
            return []

    graph = FakeGraph()
    projector = BoardProjector(board)
    assert projector.sync(graph) == 1
    assert projector.sync(graph) == 1
    findings = board.query_findings(FindingPredicate())
    assert len(findings) == 1
    assert findings[0].kind == FindingKind.HTTP_ENDPOINT


def test_reason_swarm_uses_graph_summary():
    import asyncio

    from dswarm.swarm.reason_scheduler import ReasonSwarm
    from dswarm.solver.reason import ReasonResult

    seen: list[str] = []

    class FakeGraph:
        def to_reason_summary(self, standing_guidance=None):
            return "GRAPH: verified fact #1"

    async def reason_fn(summary, challenge_id):
        seen.append(summary)
        return ReasonResult(goal_met=True, intents=[], audit_notes=[])

    async def worker(decision, profile):
        return SimpleNamespace(flag=None, flags=[], engine="pi-web")

    swarm = ReasonSwarm(
        SimpleNamespace(id="c1", name="c1", category="web", target="http://x", expected_flags=1),
        board=MemoryBoard("c1"),
        graph=FakeGraph(),
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    asyncio.run(swarm.run())
    assert seen and seen[0] == "GRAPH: verified fact #1"


class _CaptureBus:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)


class _ListGraph:
    def __init__(self, events):
        self._events = list(events)

    def events_since(self, after_seq, kinds=None):
        return [e for e in self._events if int(e.get("seq") or 0) > after_seq]


def test_projector_emits_finding_upserted_with_pheromone_params():
    async def main():
        board = MemoryBoard("c-bus")
        bus = _CaptureBus()
        projector = BoardProjector(
            board, bus=bus, run_id="run-1", challenge_id="c-bus"
        )
        graph = _ListGraph([
            _event(
                1,
                payload={
                    "fact": "login endpoint",
                    "finding": {
                        "kind": FindingKind.HTTP_ENDPOINT,
                        "target": "https://app.test/login",
                        "data": {"status": 200},
                        "verified": True,
                    },
                },
            ),
            _event(2, payload={"fact": "ssh banner reveals OpenSSH 9.2"}),
        ])
        assert projector.sync(graph) == 2
        await asyncio.sleep(0)
        assert len(bus.events) == 2

        base_expected, half_expected = PheromoneSettings.defaults().lookup(
            FindingKind.HTTP_ENDPOINT
        )
        ev = bus.events[0]
        assert ev.event_type is EventType.BLACKBOARD_DELTA
        assert ev.run_id == "run-1"
        assert ev.challenge_id == "c-bus"
        p = ev.payload
        assert p["kind"] == "finding_upserted"
        assert p["delta_type"] == "finding_upserted"
        assert p["actor"] == "projector"
        assert p["finding_id"] == "fact:1:base"
        assert p["projection_key"] == "fact:1:base"
        assert p["finding_kind"] == FindingKind.HTTP_ENDPOINT
        assert p["target"] == "https://app.test/login"
        assert p["payload"]["fact"] == "login endpoint"
        assert p["payload"]["status"] == 200
        assert p["source_seq"] == 1
        assert p["pheromone_base"] == base_expected
        assert p["pheromone_half_life_sec"] == half_expected
        created = p["pheromone_created_at"]
        assert isinstance(created, str) and created.endswith("Z") and "T" in created
        assert p["experimental"] is True

        base2, half2 = PheromoneSettings.defaults().lookup(FindingKind.TEXT_FACT)
        p2 = bus.events[1].payload
        assert p2["finding_kind"] == FindingKind.TEXT_FACT
        assert p2["source_seq"] == 2
        assert p2["pheromone_base"] == base2
        assert p2["pheromone_half_life_sec"] == half2

        # Re-sync with no new graph events: no further emissions.
        assert projector.sync(graph) == 2
        await asyncio.sleep(0)
        assert len(bus.events) == 2

        # A new graph event emits exactly one more delta.
        graph._events.append(_event(3, payload={"fact": "new fact"}))
        assert projector.sync(graph) == 3
        await asyncio.sleep(0)
        assert len(bus.events) == 3
        assert bus.events[2].payload["source_seq"] == 3

    asyncio.run(main())


def test_projector_emit_failure_does_not_break_sync():
    class _BadBus:
        async def emit(self, ev):
            raise RuntimeError("boom")

    async def main():
        board = MemoryBoard("c-bad")
        projector = BoardProjector(
            board, bus=_BadBus(), run_id="run-1", challenge_id="c-bad"
        )
        graph = _ListGraph([_event(1, payload={"fact": "still projected"})])
        assert projector.sync(graph) == 1
        await asyncio.sleep(0)
        findings = board.query_findings(FindingPredicate())
        assert len(findings) == 1
        assert findings[0].target == "still projected"

    asyncio.run(main())


def test_promotion_projection_replaces_once_and_replay_is_strict_noop(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    async def main():
        graph = SQLiteSharedGraph.open(
            db_path=tmp_path / "graph.db",
            challenge=Challenge(id="c-promote", name="c", category="web"),
        )
        fact_seq = graph.add_evidence(
            actor="pi-web", source="curl", fact="admin endpoint", verified=False,
            confidence=0.4,
            finding=__import__("dswarm.swarm.board", fromlist=["StructuredFinding"]).StructuredFinding(
                kind=FindingKind.HTTP_ENDPOINT, target="https://app/admin",
                data={"status": 200},
            ),
        )
        graph.add_evidence(
            actor="pi-web", source="curl", fact="admin endpoint", verified=True,
            confidence=0.93, artifact_id="art-proof", witness="curl 200", verifier="gate",
        )
        promotion_seq = graph.events_since(0, kinds=("fact_verified",))[0]["seq"]

        board = MemoryBoard("c-promote")
        bus = _CaptureBus()
        projector = BoardProjector(board, bus=bus, run_id="run-promote", challenge_id="c-promote")
        assert projector.sync(graph) == promotion_seq
        await asyncio.sleep(0)

        active = board.query_findings(FindingPredicate())
        assert len(active) == 1
        assert active[0].source_seq == fact_seq
        assert active[0].payload["verified"] is True
        assert active[0].payload["promotion"]["seq"] == promotion_seq
        assert len(bus.events) == 2
        assert bus.events[-1].payload["supersedes_source_seq"] == fact_seq
        assert bus.events[-1].payload["transition_seq"] == promotion_seq

        # Cold replay into the same board must be a strict no-op for both rows and deltas.
        replay = BoardProjector(board, after_seq=0, bus=bus, run_id="run-promote", challenge_id="c-promote")
        assert replay.sync(graph) == promotion_seq
        await asyncio.sleep(0)
        assert len(board.query_findings(FindingPredicate())) == 1
        assert len(bus.events) == 2
        graph.close()

    asyncio.run(main())


def test_promotion_partial_sync_without_base_does_not_claim_supersession(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.board import StructuredFinding
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    async def main():
        graph = SQLiteSharedGraph.open(
            db_path=tmp_path / "partial-promotion.db",
            challenge=Challenge(id="c-partial", name="c", category="web"),
        )
        fact_seq = graph.add_evidence(
            actor="pi-web", source="curl", fact="admin endpoint", verified=False,
            finding=StructuredFinding(kind=FindingKind.HTTP_ENDPOINT, target="https://app/admin"),
        )
        graph.add_evidence(
            actor="pi-web", source="curl", fact="admin endpoint", verified=True,
            confidence=0.93, artifact_id="art-proof", witness="curl 200", verifier="gate",
        )
        promotion_seq = graph.events_since(0, kinds=("fact_verified",))[0]["seq"]

        board = MemoryBoard("c-partial")
        bus = _CaptureBus()
        projector = BoardProjector(
            board, after_seq=fact_seq, bus=bus, run_id="run-partial", challenge_id="c-partial"
        )
        assert projector.sync(graph) == promotion_seq
        await asyncio.sleep(0)
        assert len(board.query_findings(FindingPredicate())) == 1
        assert len(bus.events) == 1
        assert "supersedes_source_seq" not in bus.events[0].payload
        assert bus.events[0].payload["transition_seq"] == promotion_seq
        graph.close()

    asyncio.run(main())


def test_projector_cursor_stays_before_failed_event():
    class _FailingBoard(MemoryBoard):
        def replace_by_source(self, *, source_seq, finding, projection_key):
            if int(source_seq) == 2:
                raise RuntimeError("projection write failed")
            return super().replace_by_source(
                source_seq=source_seq, finding=finding, projection_key=projection_key
            )

    board = _FailingBoard("c-fail")
    graph = _ListGraph([
        _event(1, payload={"fact": "first"}),
        _event(2, payload={"fact": "second"}),
    ])
    projector = BoardProjector(board)
    try:
        projector.sync(graph)
    except RuntimeError as exc:
        assert str(exc) == "projection write failed"
    else:
        raise AssertionError("sync must surface projection failures")
    assert projector.after_seq == 1
    assert [f.target for f in board.query_findings(FindingPredicate())] == ["first"]


def test_finding_pheromone_uses_origin_timestamp_with_legacy_fallback():
    routed = Finding(
        challenge_id="c-time",
        kind=FindingKind.TEXT_FACT,
        pheromone_base=1.0,
        half_life_sec=100,
        created_at=900.0,
        pheromone_origin_ts=1000.0,
    )
    legacy = Finding(
        challenge_id="c-time",
        kind=FindingKind.TEXT_FACT,
        pheromone_base=1.0,
        half_life_sec=100,
        created_at=900.0,
    )

    assert routed.pheromone(now=1100.0) == 0.5
    assert legacy.pheromone(now=1000.0) == 0.5


def test_memory_board_replace_preserves_m6_lineage_and_time_fields():
    board = MemoryBoard("c-m6", now=lambda: 2000.0)
    finding = Finding(
        challenge_id="c-m6",
        kind=FindingKind.HTTP_ENDPOINT,
        agent_name="pi-web",
        target="https://target/admin",
        route_hash="route-web",
        route_lineage="inherited",
        event_ts=1100.0,
        projected_at=1200.0,
        pheromone_origin_ts=1100.0,
        fact_origin_ts=1000.0,
        source_seq=17,
    )

    result = board.replace_by_source(
        source_seq=17,
        finding=finding,
        projection_key="fact:17:promotion:22",
    )

    stored = result.finding
    assert stored.route_hash == "route-web"
    assert stored.route_lineage == "inherited"
    assert stored.event_ts == 1100.0
    assert stored.projected_at == 1200.0
    assert stored.pheromone_origin_ts == 1100.0
    assert stored.fact_origin_ts == 1000.0


def test_projector_persists_route_lineage_and_canonical_event_times(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.board import StructuredFinding
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    graph = SQLiteSharedGraph.open(
        db_path=tmp_path / "m6-projection.db",
        challenge=Challenge(id="c-m6-project", name="c", category="web"),
    )
    graph.propose_intent(
        actor="reason",
        intent_id="I-route",
        goal="inspect admin",
        payload={"route_hash": "web-admin"},
    )
    fact_seq = graph.add_evidence(
        actor="pi-web",
        source="curl",
        fact="admin endpoint",
        verified=False,
        confidence=0.4,
        intent_id="I-route",
        finding=StructuredFinding(
            kind=FindingKind.HTTP_ENDPOINT,
            target="https://target/admin",
        ),
    )
    base_event = next(
        event for event in graph.events_since(0, kinds=("fact_added",))
        if int(event["seq"]) == fact_seq
    )
    observation = graph.route_lineage_for_fact(fact_seq)

    board = MemoryBoard("c-m6-project", now=lambda: 5000.0)
    projector = BoardProjector(board, challenge_id="c-m6-project")
    assert projector.sync(graph) == fact_seq
    base = board.query_findings(FindingPredicate())[0]
    assert base.route_hash == observation.effective_route_hash
    assert base.route_lineage == "inherited"
    assert base.event_ts == float(base_event["ts"])
    assert base.pheromone_origin_ts == float(base_event["ts"])
    assert base.fact_origin_ts == float(base_event["ts"])
    assert base.projected_at == 5000.0

    graph.add_evidence(
        actor="pi-web",
        source="curl",
        fact="admin endpoint",
        verified=True,
        confidence=0.95,
        artifact_id="art-proof",
        witness="HTTP 200",
        verifier="gate",
    )
    promotion_event = graph.events_since(fact_seq, kinds=("fact_verified",))[0]
    assert projector.sync(graph) == int(promotion_event["seq"])
    promoted = board.query_findings(FindingPredicate())[0]
    assert promoted.route_hash == observation.effective_route_hash
    assert promoted.route_lineage == "inherited"
    assert promoted.event_ts == float(promotion_event["ts"])
    assert promoted.pheromone_origin_ts == float(promotion_event["ts"])
    assert promoted.fact_origin_ts == float(base_event["ts"])
    assert promoted.projected_at == 5000.0
    graph.close()


def test_projector_cold_replay_preserves_m6_semantics(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    graph = SQLiteSharedGraph.open(
        db_path=tmp_path / "m6-replay.db",
        challenge=Challenge(id="c-m6-replay", name="c", category="misc"),
    )
    fact_seq = graph.add_evidence(
        actor="pi-misc",
        source="stdout",
        fact="stable replay fact",
        verified=False,
        route_hash="misc-replay",
    )
    graph.add_evidence(
        actor="verifier",
        source="stdout",
        fact="stable replay fact",
        verified=True,
        confidence=0.9,
        artifact_id="art-replay",
        witness="confirmed",
        verifier="gate",
    )

    online_board = MemoryBoard("c-m6-replay", now=lambda: 7000.0)
    online = BoardProjector(online_board, challenge_id="c-m6-replay")
    assert online.sync(graph) > fact_seq

    replay_board = MemoryBoard("c-m6-replay", now=lambda: 7000.0)
    replay = BoardProjector(replay_board, challenge_id="c-m6-replay")
    assert replay.sync(graph) == online.after_seq

    online_finding = online_board.query_findings(FindingPredicate())[0]
    replay_finding = replay_board.query_findings(FindingPredicate())[0]
    fields = (
        "route_hash", "route_lineage", "event_ts", "projected_at",
        "pheromone_origin_ts", "fact_origin_ts",
    )
    assert {name: getattr(online_finding, name) for name in fields} == {
        name: getattr(replay_finding, name) for name in fields
    }
    graph.close()
