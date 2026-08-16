"""Project append-only SharedGraph events into the pheromone Board."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.swarm.board import (
    Board, Finding, FindingKind, ReplacementOutcome, StructuredFinding,
)


class BoardProjector:
    """Idempotent SharedGraph -> Board projection keyed by event seq.

    The shared graph remains the append-only source of truth; this class only
    materializes the small finding/pheromone view ReasonSwarm and workers use.

    When a bus is wired, every newly projected finding also emits one
    BLACKBOARD_DELTA (``finding_upserted``, docs/07 §7.1) carrying the
    finding's immutable pheromone parameters so the deck can replay decay
    with the kernel's own half-life formula. Emission is best-effort: a
    missing bus or a bus failure never affects the projection itself.
    """

    def __init__(
        self,
        board: Board,
        *,
        after_seq: int = 0,
        bus: Optional[Any] = None,
        run_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
    ) -> None:
        self.board = board
        self.after_seq = int(after_seq or 0)
        self.bus = bus
        self.run_id = run_id
        self.challenge_id = challenge_id
        self._emit_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _event_timestamp(ev: dict[str, Any]) -> Optional[float]:
        value = ev.get("ts")
        if value is None:
            return None
        converter = getattr(value, "timestamp", None)
        try:
            return float(converter() if callable(converter) else value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _route_observation(graph: Any, fact_seq: int) -> Optional[Any]:
        resolver = getattr(graph, "route_lineage_for_fact", None)
        if not callable(resolver):
            return None
        try:
            return resolver(int(fact_seq))
        except ValueError:
            return None

    @staticmethod
    def _apply_route_observation(finding: Finding, observation: Optional[Any]) -> None:
        if observation is None:
            return
        finding.route_hash = str(observation.effective_route_hash or "")
        finding.route_lineage = str(observation.lineage or "unattributed")

    @staticmethod
    def project_event(ev: dict[str, Any]) -> Optional[Finding]:
        kind = str(ev.get("kind") or "")
        if kind != "fact_added":
            return None
        payload = dict(ev.get("payload") or {})
        fact = str(payload.get("fact") or "")
        if not fact:
            return None
        actor = str(ev.get("actor") or "worker")
        event_ts = BoardProjector._event_timestamp(ev)
        verified = bool(ev.get("verified"))
        confidence = float(ev.get("confidence") or 0.4)
        finding = StructuredFinding.from_dict(payload.get("finding"))
        if finding is not None:
            target = finding.target or fact
            data = {
                "fact": fact,
                "verified": verified,
                "confidence": confidence,
                "source": finding.source or payload.get("source") or "",
                "artifact_id": finding.artifact_id or ev.get("artifact_id") or "",
                "witness": finding.witness or payload.get("witness") or "",
                **dict(finding.data or {}),
            }
            return Finding(
                challenge_id=str(ev.get("challenge_id") or payload.get("challenge_id") or ""),
                kind=finding.kind,
                agent_name=actor,
                target=target,
                payload=data,
                source_seq=int(ev.get("seq") or 0),
                event_ts=event_ts,
                pheromone_origin_ts=event_ts,
                fact_origin_ts=event_ts,
            )
        return Finding(
            challenge_id=str(ev.get("challenge_id") or payload.get("challenge_id") or ""),
            kind=FindingKind.TEXT_FACT,
            agent_name=actor,
            target=fact,
            payload={
                "fact": fact,
                "verified": verified,
                "confidence": confidence,
                "source": payload.get("source") or "",
                "artifact_id": ev.get("artifact_id") or "",
                "witness": payload.get("witness") or "",
            },
            source_seq=int(ev.get("seq") or 0),
            event_ts=event_ts,
            pheromone_origin_ts=event_ts,
            fact_origin_ts=event_ts,
        )

    @staticmethod
    def _project_effective_fact(
        item: dict[str, Any],
        promotion_seq: int,
        *,
        promotion_ts: Optional[float] = None,
        observation: Optional[Any] = None,
    ) -> Optional[Finding]:
        fact = str(item.get("fact_text") or "")
        if not fact:
            return None
        finding_kind = str(item.get("finding_kind") or "") or FindingKind.TEXT_FACT
        target = str(item.get("finding_target") or "") or fact
        data = {
            "fact": fact,
            "verified": bool(item.get("verified")),
            "confidence": float(item.get("confidence") or 0.0),
            "source": str(item.get("source") or item.get("fact_source") or ""),
            "artifact_id": str(item.get("artifact_id") or ""),
            "witness": str(item.get("witness") or ""),
            **dict(item.get("finding_data") or {}),
            "promotion": {
                "seq": int(promotion_seq),
                "actor": str(item.get("promotion_actor") or ""),
                "witness": str(item.get("witness") or ""),
                "verifier": str(item.get("verifier") or ""),
                "source": str(item.get("source") or item.get("fact_source") or ""),
                "artifact_id": str(item.get("promotion_artifact_id") or ""),
            },
        }
        fact_origin_ts = (
            float(item["fact_ts"]) if item.get("fact_ts") is not None else None
        )
        return Finding(
            challenge_id=str(item.get("challenge_id") or ""),
            kind=finding_kind,
            agent_name=str(item.get("promotion_actor") or item.get("fact_actor") or "worker"),
            target=target,
            payload=data,
            source_seq=int(item.get("fact_seq") or 0),
            route_hash=(
                str(observation.effective_route_hash or "")
                if observation is not None else ""
            ),
            route_lineage=(
                str(observation.lineage or "unattributed")
                if observation is not None else ""
            ),
            event_ts=promotion_ts,
            pheromone_origin_ts=promotion_ts,
            fact_origin_ts=fact_origin_ts,
        )

    def sync(self, graph: Any) -> int:
        events = graph.events_since(
            self.after_seq, kinds=("fact_added", "fact_verified")
        )
        for ev in events:
            seq = int(ev.get("seq") or 0)
            if seq <= self.after_seq:
                continue
            kind = str(ev.get("kind") or "")
            if kind == "fact_verified":
                payload = dict(ev.get("payload") or {})
                fact_seq = int(payload.get("fact_seq") or 0)
                effective = graph.effective_fact(fact_seq)
                observation = self._route_observation(graph, fact_seq)
                projected = (
                    self._project_effective_fact(
                        effective,
                        seq,
                        promotion_ts=self._event_timestamp(ev),
                        observation=observation,
                    )
                    if effective is not None else None
                )
                if projected is not None:
                    projected.challenge_id = projected.challenge_id or getattr(
                        self.board, "challenge_id", ""
                    )
                    projection_key = f"fact:{fact_seq}:promotion:{seq}"
                    result = self.board.replace_by_source(
                        source_seq=fact_seq,
                        finding=projected,
                        projection_key=projection_key,
                    )
                    if result.outcome != ReplacementOutcome.ALREADY_APPLIED:
                        transition = {
                            "transition_seq": seq,
                            "projection_key": projection_key,
                        }
                        if result.outcome == ReplacementOutcome.REPLACED:
                            transition["supersedes_source_seq"] = fact_seq
                        self._emit_upsert(result.finding, fact_seq, **transition)
            else:
                projected = self.project_event(ev)
                if projected is not None:
                    observation = self._route_observation(graph, projected.source_seq or seq)
                    self._apply_route_observation(projected, observation)
                    if observation is not None:
                        projected.event_ts = float(observation.event_ts)
                        projected.pheromone_origin_ts = float(observation.event_ts)
                        projected.fact_origin_ts = float(observation.event_ts)
                    projected.challenge_id = projected.challenge_id or getattr(
                        self.board, "challenge_id", ""
                    )
                    fact_seq = int(projected.source_seq or seq)
                    projection_key = f"fact:{fact_seq}:base"
                    result = self.board.replace_by_source(
                        source_seq=fact_seq,
                        finding=projected,
                        projection_key=projection_key,
                    )
                    if result.outcome != ReplacementOutcome.ALREADY_APPLIED:
                        transition = {"projection_key": projection_key}
                        if result.outcome == ReplacementOutcome.REPLACED:
                            transition["supersedes_source_seq"] = fact_seq
                        self._emit_upsert(result.finding, fact_seq, **transition)
            # Advance only after this event was fully projected (or deliberately
            # skipped as malformed). Exceptions leave the cursor at the last
            # successfully processed event so replay is safe.
            self.after_seq = seq
        return self.after_seq

    def _emit_upsert(
        self,
        finding: Finding,
        source_seq: int,
        **transition: Any,
    ) -> None:
        """Fire-and-forget ``finding_upserted`` delta (docs/07 §7.1).

        Carries only immutable parameters (base / half-life / created_at);
        the deck computes current strength with the kernel's half-life
        formula. Scheduled on the running loop when one exists; silently
        skipped otherwise. Never raises into ``sync``.
        """
        if self.bus is None:
            return
        try:
            created_at = (
                datetime.fromtimestamp(
                    float(
                        finding.pheromone_origin_ts
                        if finding.pheromone_origin_ts is not None
                        else finding.created_at
                    ),
                    tz=timezone.utc,
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            event = Event(
                event_type=EventType.BLACKBOARD_DELTA,
                run_id=self.run_id or self.challenge_id or finding.challenge_id or "",
                challenge_id=self.challenge_id or finding.challenge_id or None,
                payload=blackboard_delta_payload(
                    "finding_upserted",
                    actor="projector",
                    delta_type="finding_upserted",
                    finding_id=finding.finding_id,
                    finding_kind=finding.kind,
                    target=finding.target,
                    payload=dict(finding.payload or {}),
                    source_seq=int(source_seq),
                    pheromone_base=float(finding.pheromone_base),
                    pheromone_half_life_sec=int(finding.half_life_sec),
                    pheromone_created_at=created_at,
                    experimental=True,
                    **transition,
                ),
            )
            task = asyncio.get_running_loop().create_task(self._emit_safe(event))
            self._emit_tasks.add(task)
            task.add_done_callback(self._emit_tasks.discard)
        except Exception:
            pass

    async def _emit_safe(self, event: Event) -> None:
        try:
            await self.bus.emit(event)
        except Exception:
            pass
