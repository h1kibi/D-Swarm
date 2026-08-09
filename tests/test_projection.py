"""Structured finding and SharedGraph -> Board projection tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dswarm.core.events import EventType
from dswarm.swarm.board import (
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
        assert p["finding_id"] == "finding:1"
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
