"""Project append-only SharedGraph events into the pheromone Board."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.swarm.board import Board, Finding, FindingKind, StructuredFinding


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
    def project_event(ev: dict[str, Any]) -> Optional[Finding]:
        kind = str(ev.get("kind") or "")
        if kind != "fact_added":
            return None
        payload = dict(ev.get("payload") or {})
        fact = str(payload.get("fact") or "")
        if not fact:
            return None
        actor = str(ev.get("actor") or "worker")
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
        )

    def sync(self, graph: Any) -> int:
        events = graph.events_since(self.after_seq, kinds=("fact_added",))
        last = self.after_seq
        for ev in events:
            seq = int(ev.get("seq") or 0)
            if seq <= self.after_seq:
                continue
            projected = self.project_event(ev)
            if projected is not None:
                projected.challenge_id = projected.challenge_id or getattr(
                    self.board, "challenge_id", ""
                )
                finding = self.board.write_finding(
                    challenge_id=projected.challenge_id,
                    kind=projected.kind,
                    agent_name=projected.agent_name,
                    target=projected.target,
                    payload=projected.payload,
                    source_seq=seq,
                )
                self._emit_upsert(finding, seq)
            last = max(last, seq)
        self.after_seq = last
        return last

    def _emit_upsert(self, finding: Finding, source_seq: int) -> None:
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
                datetime.fromtimestamp(float(finding.created_at), tz=timezone.utc)
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
