from __future__ import annotations

import asyncio
from pathlib import Path

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.event_reader import GraphEventReader
from dswarm.swarm.shared_graph import EV_DEAD_END, EV_FACT_ADDED, SQLiteSharedGraph


def _challenge() -> Challenge:
    return Challenge(
        id="event-reader-snapshot",
        name="event reader snapshot",
        category="web",
        mode="ctf",
    )


def test_event_reader_preserves_query_and_polling_contract(tmp_path: Path) -> None:
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    try:
        assert isinstance(graph._event_reader, GraphEventReader)
        fact_seq = graph.add_evidence(
            actor="worker-a", source="worker-a",
            fact="the target returned a deterministic response",
            verified=True, confidence=0.8,
        )
        dead_seq = graph.add_dead_end(actor="worker-a", reason="no flag here")

        all_events = graph.events()
        assert [event["seq"] for event in all_events[-2:]] == [fact_seq, dead_seq]
        assert all_events[-2]["kind"] == EV_FACT_ADDED
        assert all_events[-1]["kind"] == EV_DEAD_END
        assert all_events[-2]["verified"] is True
        assert all_events[-2]["payload"]["fact"] == (
            "the target returned a deterministic response"
        )

        assert [event["kind"] for event in graph.events_since(fact_seq)] == [EV_DEAD_END]
        assert [event["kind"] for event in graph.events_since(0, kinds=[EV_DEAD_END])] == [EV_DEAD_END]
        assert graph.recent_events(0) == []
        assert [event["seq"] for event in graph.recent_events(1)] == [dead_seq]

        async def read_one() -> dict:
            stream = graph.subscribe_events(after_seq=fact_seq, poll_interval=0.05)
            try:
                return await anext(stream)
            finally:
                await stream.aclose()

        assert asyncio.run(read_one())["seq"] == dead_seq
    finally:
        graph.close()
