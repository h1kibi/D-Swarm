"""Deterministic offline replay helpers for M6 route observations.

The replay consumes immutable graph facts.  It does not read the telemetry
sidecar and does not replace the production projector's full lifecycle replay.
"""

from __future__ import annotations

import math
from typing import Any

from dswarm.swarm.board import MemoryBoard
from dswarm.swarm.projection import BoardProjector


class ReplayClock:
    """Small mutable clock used only by offline Board replay."""

    def __init__(self, initial: float = 0.0) -> None:
        self._value = 0.0
        self.set(initial)

    def set(self, ts: float) -> None:
        value = float(ts)
        if not math.isfinite(value):
            raise ValueError("replay timestamp must be finite")
        self._value = value

    def now(self) -> float:
        return self._value


def replay_route_observations(graph: Any) -> MemoryBoard:
    """Project graph fact observations in stable virtual-time order.

    This is the deterministic input harness for M7 experiments: route identity
    comes from ``RouteObservation`` built from the immutable graph, while the
    sidecar metrics artifact is intentionally ignored.
    """

    events = list(graph.events_since(0, kinds=("fact_added",)))
    by_seq = {
        int(event.get("seq") or 0): event
        for event in events
        if int(event.get("seq") or 0) > 0
    }
    observations = graph.route_observations(sorted(by_seq)) if by_seq else {}
    ordered = sorted(
        observations.values(),
        key=lambda observation: (float(observation.event_ts), int(observation.fact_seq)),
    )
    challenge = getattr(graph, "challenge", None)
    challenge_id = str(getattr(challenge, "id", "") or "")
    clock = ReplayClock()
    board = MemoryBoard(challenge_id, now=clock.now)

    for observation in ordered:
        event = by_seq.get(int(observation.fact_seq))
        if event is None:
            continue
        projected = BoardProjector.project_event(event)
        if projected is None:
            continue
        clock.set(float(observation.event_ts))
        projected.challenge_id = projected.challenge_id or challenge_id
        projected.route_hash = str(observation.effective_route_hash or "")
        projected.route_lineage = str(observation.lineage or "unattributed")
        projected.event_ts = float(observation.event_ts)
        projected.projected_at = None
        projected.pheromone_origin_ts = float(observation.event_ts)
        projected.fact_origin_ts = float(observation.event_ts)
        fact_seq = int(observation.fact_seq)
        board.replace_by_source(
            source_seq=fact_seq,
            finding=projected,
            projection_key=f"fact:{fact_seq}:base",
        )
    return board
