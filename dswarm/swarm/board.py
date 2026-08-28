"""Stigmergic blackboard primitives for the Reason-centered swarm.

The board is the shared environment that agents write findings into and the
scheduler/reason read from. It intentionally mirrors the board interface used
by Pentest-Swarm-AI while staying small enough to be implemented by both an
in-memory fake and PostgreSQL.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


class FindingKind:
    TARGET_REGISTERED = "TARGET_REGISTERED"
    TEXT_FACT = "TEXT_FACT"
    ATTACK_SURFACE = "ATTACK_SURFACE"
    SUBDOMAIN = "SUBDOMAIN"
    PORT_OPEN = "PORT_OPEN"
    SERVICE = "SERVICE"
    HTTP_ENDPOINT = "HTTP_ENDPOINT"
    TECHNOLOGY = "TECHNOLOGY"
    CVE_MATCH = "CVE_MATCH"
    MISCONFIGURATION = "MISCONFIGURATION"
    SECRET_LEAK = "SECRET_LEAK"
    POTENTIAL_VULN = "POTENTIAL_VULN"
    EXPLOIT_CHAIN = "EXPLOIT_CHAIN"
    EXPLOIT_RESULT = "EXPLOIT_RESULT"
    SESSION = "SESSION"
    NEW_SURFACE = "NEW_SURFACE"
    FLAG_FOUND = "FLAG_FOUND"
    CAMPAIGN_COMPLETE = "CAMPAIGN_COMPLETE"
    AGENT_ERROR = "AGENT_ERROR"


@dataclass
class Finding:
    challenge_id: str
    kind: str
    agent_name: str = "engine"
    target: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    pheromone_base: float = 1.0
    half_life_sec: int = 3600
    embedding: Optional[list[float]] = None
    source_seq: int = 0
    projection_key: str = ""
    route_hash: str = ""
    route_lineage: str = ""
    event_ts: Optional[float] = None
    projected_at: Optional[float] = None
    pheromone_origin_ts: Optional[float] = None
    fact_origin_ts: Optional[float] = None
    superseded_by: Optional[str] = None
    seq: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def finding_id(self) -> str:
        return self.projection_key or f"finding:{self.seq}"

    def pheromone(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        origin = (
            self.pheromone_origin_ts
            if self.pheromone_origin_ts is not None
            else self.created_at
        )
        age = max(0.0, now - origin)
        half = max(1, int(self.half_life_sec))
        return max(0.0, min(1.0, float(self.pheromone_base) * (0.5 ** (age / half))))


class ReplacementOutcome(str, Enum):
    REPLACED = "replaced"
    INSERTED_NO_PRIOR = "inserted_no_prior"
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True)
class ReplacementResult:
    outcome: ReplacementOutcome
    finding: Finding
    superseded_ids: tuple[str, ...] = ()


@dataclass
class StructuredFinding:
    """Structured finding carried inside a fact_added event."""

    kind: str
    target: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    source: str = ""
    artifact_id: str = ""
    witness: str = ""
    confidence: float = 0.4
    intent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "data": dict(self.data or {}),
            "verified": self.verified,
            "source": self.source,
            "artifact_id": self.artifact_id,
            "witness": self.witness,
            "confidence": self.confidence,
            "intent_id": self.intent_id,
        }

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> Optional["StructuredFinding"]:
        if not raw:
            return None
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").strip()
        if not kind:
            return None
        return cls(
            kind=kind,
            target=str(raw.get("target") or ""),
            data=dict(raw.get("data") or {}),
            verified=bool(raw.get("verified")),
            source=str(raw.get("source") or ""),
            artifact_id=str(raw.get("artifact_id") or ""),
            witness=str(raw.get("witness") or ""),
            confidence=float(raw.get("confidence") or 0.4),
            intent_id=str(raw.get("intent_id") or ""),
        )


@dataclass
class FindingPredicate:
    kinds: tuple[str, ...] = ()
    target_prefix: str = ""
    min_pheromone: float = 0.0
    since_seq: int = 0
    limit: int = 0

    def matches(self, f: Finding, now: Optional[float] = None) -> bool:
        if self.kinds and f.kind not in self.kinds:
            return False
        if self.target_prefix and not f.target.startswith(self.target_prefix):
            return False
        if f.seq <= self.since_seq:
            return False
        if self.min_pheromone > 0 and f.pheromone(now=now) < self.min_pheromone:
            return False
        return True


@runtime_checkable
class Board(Protocol):
    def write_finding(
        self,
        *,
        challenge_id: str,
        kind: str,
        agent_name: str = "engine",
        target: str = "",
        payload: Optional[dict[str, Any]] = None,
        pheromone_base: float = 1.0,
        half_life_sec: int = 3600,
        embedding: Optional[list[float]] = None,
        source_seq: int = 0,
        projection_key: str = "",
        route_hash: str = "",
        route_lineage: str = "",
        event_ts: Optional[float] = None,
        projected_at: Optional[float] = None,
        pheromone_origin_ts: Optional[float] = None,
        fact_origin_ts: Optional[float] = None,
    ) -> Finding: ...

    def query_findings(self, predicate: FindingPredicate) -> list[Finding]: ...

    async def subscribe(self, predicate: FindingPredicate) -> AsyncIterator[Finding]: ...

    def cursor(self, challenge_id: str, agent_name: str) -> int: ...

    def commit_cursor(self, challenge_id: str, agent_name: str, last_seq: int) -> None: ...

    def supersede(self, old_id: str, new_id: str) -> None: ...

    def replace_by_source(
        self, *, source_seq: int, finding: Finding, projection_key: str
    ) -> ReplacementResult: ...

    def budget(self, challenge_id: str) -> dict[str, Any]: ...

    def update_budget(
        self, challenge_id: str, delta_hours: float = 0.0, delta_tokens: int = 0
    ) -> None: ...

    def agent_budget(self, challenge_id: str, agent_name: str) -> dict[str, Any]: ...

    def charge_agent(
        self, challenge_id: str, agent_name: str, tokens: int = 0
    ) -> None: ...


@dataclass
class PheromoneEntry:
    base: float = 0.5
    half_life_sec: int = 3600


@dataclass
class PheromoneSettings:
    types: dict[str, PheromoneEntry] = field(default_factory=dict)
    default: PheromoneEntry = field(default_factory=PheromoneEntry)
    bias: float = 1.0

    def lookup(self, kind: str) -> tuple[float, int]:
        entry = self.types.get(kind, self.default)
        return max(0.0, min(1.0, entry.base * self.bias)), entry.half_life_sec

    def with_bias(self, bias: float) -> "PheromoneSettings":
        return PheromoneSettings(
            types={k: v for k, v in self.types.items()},
            default=self.default,
            bias=bias,
        )

    @classmethod
    def defaults(cls) -> "PheromoneSettings":
        types = {
            FindingKind.TARGET_REGISTERED: PheromoneEntry(1.0, 24 * 3600),
            FindingKind.TEXT_FACT: PheromoneEntry(0.5, 2 * 3600),
            FindingKind.ATTACK_SURFACE: PheromoneEntry(0.8, 12 * 3600),
            FindingKind.SUBDOMAIN: PheromoneEntry(0.7, 2 * 3600),
            FindingKind.PORT_OPEN: PheromoneEntry(0.8, 3600),
            FindingKind.SERVICE: PheromoneEntry(0.8, 3600),
            FindingKind.HTTP_ENDPOINT: PheromoneEntry(0.6, 6 * 3600),
            FindingKind.TECHNOLOGY: PheromoneEntry(0.5, 2 * 3600),
            FindingKind.CVE_MATCH: PheromoneEntry(1.0, 3 * 3600),
            FindingKind.MISCONFIGURATION: PheromoneEntry(0.6, 3600),
            FindingKind.SECRET_LEAK: PheromoneEntry(0.9, 2 * 3600),
            FindingKind.POTENTIAL_VULN: PheromoneEntry(0.7, 3600),
            FindingKind.EXPLOIT_CHAIN: PheromoneEntry(0.9, 3600),
            FindingKind.EXPLOIT_RESULT: PheromoneEntry(1.0, 1800),
            FindingKind.SESSION: PheromoneEntry(0.8, 900),
            FindingKind.NEW_SURFACE: PheromoneEntry(0.9, 6 * 3600),
            FindingKind.FLAG_FOUND: PheromoneEntry(1.0, 0),
            FindingKind.CAMPAIGN_COMPLETE: PheromoneEntry(1.0, 300),
            FindingKind.AGENT_ERROR: PheromoneEntry(0.3, 600),
        }
        return cls(types=types, default=PheromoneEntry(0.5, 3600))


class MemoryBoard:
    """In-memory Board implementation for tests and local development."""

    def __init__(
        self,
        challenge_id: str = "c1",
        *,
        pheromone: Optional[PheromoneSettings] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.challenge_id = challenge_id
        self.pheromone = pheromone or PheromoneSettings.defaults()
        self._now = now or time.time
        self._findings: list[Finding] = []
        self._projection_index: dict[str, Finding] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._budgets: dict[str, dict[str, Any]] = {}
        self._agent_budgets: dict[tuple[str, str], dict[str, Any]] = {}
        self._subscribers: list[tuple[FindingPredicate, asyncio.Queue[Finding]]] = []
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def write_finding(
        self,
        *,
        challenge_id: str,
        kind: str,
        agent_name: str = "engine",
        target: str = "",
        payload: Optional[dict[str, Any]] = None,
        pheromone_base: float = 1.0,
        half_life_sec: int = 3600,
        embedding: Optional[list[float]] = None,
        source_seq: int = 0,
        projection_key: str = "",
        route_hash: str = "",
        route_lineage: str = "",
        event_ts: Optional[float] = None,
        projected_at: Optional[float] = None,
        pheromone_origin_ts: Optional[float] = None,
        fact_origin_ts: Optional[float] = None,
    ) -> Finding:
        key = str(projection_key or "")
        if key and key in self._projection_index:
            return self._projection_index[key]
        if kind == FindingKind.FLAG_FOUND:
            value = str(target or (payload or {}).get("flag") or "")
            existing = [
                f for f in self._findings
                if f.kind == FindingKind.FLAG_FOUND
                and (f.target == value or (f.payload or {}).get("flag") == value)
            ]
            if existing:
                return existing[0]
        base, half = self.pheromone.lookup(kind)
        created_at = float(self._now())
        f = Finding(
            challenge_id=challenge_id,
            kind=kind,
            agent_name=agent_name,
            target=target,
            payload=dict(payload or {}),
            pheromone_base=base if pheromone_base == 1.0 else max(0.0, min(1.0, pheromone_base)),
            half_life_sec=half if half_life_sec == 3600 else half_life_sec,
            embedding=embedding,
            source_seq=source_seq,
            projection_key=key,
            route_hash=str(route_hash or ""),
            route_lineage=str(route_lineage or ""),
            event_ts=float(event_ts) if event_ts is not None else None,
            projected_at=(
                float(projected_at) if projected_at is not None else created_at
            ),
            pheromone_origin_ts=(
                float(pheromone_origin_ts)
                if pheromone_origin_ts is not None else None
            ),
            fact_origin_ts=(
                float(fact_origin_ts) if fact_origin_ts is not None else None
            ),
            seq=self._next_seq(),
            created_at=created_at,
        )
        self._findings.append(f)
        if key:
            self._projection_index[key] = f
        for pred, q in list(self._subscribers):
            if pred.matches(f):
                q.put_nowait(f)
        return f

    def query_findings(self, predicate: FindingPredicate) -> list[Finding]:
        out = [
            f for f in self._findings
            if f.superseded_by is None and predicate.matches(f, now=self._now())
        ]
        out.sort(key=lambda f: f.seq, reverse=True)
        if predicate.limit > 0:
            return out[: predicate.limit]
        return out

    async def subscribe(self, predicate: FindingPredicate) -> AsyncIterator[Finding]:
        q: asyncio.Queue[Finding] = asyncio.Queue()
        self._subscribers.append((predicate, q))
        for f in self.query_findings(predicate):
            q.put_nowait(f)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers = [
                (p, s) for p, s in self._subscribers if s is not q
            ]

    def cursor(self, challenge_id: str, agent_name: str) -> int:
        return self._cursors.get((challenge_id, agent_name), 0)

    def commit_cursor(self, challenge_id: str, agent_name: str, last_seq: int) -> None:
        self._cursors[(challenge_id, agent_name)] = max(
            self._cursors.get((challenge_id, agent_name), 0), int(last_seq)
        )

    def supersede(self, old_id: str, new_id: str) -> None:
        for f in self._findings:
            if f.finding_id == old_id:
                f.superseded_by = new_id

    def replace_by_source(
        self, *, source_seq: int, finding: Finding, projection_key: str
    ) -> ReplacementResult:
        key = str(projection_key or "")
        existing = self._projection_index.get(key) if key else None
        if existing is not None:
            return ReplacementResult(ReplacementOutcome.ALREADY_APPLIED, existing)
        prior = [
            item for item in self._findings
            if item.source_seq == int(source_seq) and item.superseded_by is None
        ]
        replacement = self.write_finding(
            challenge_id=finding.challenge_id or self.challenge_id, kind=finding.kind,
            agent_name=finding.agent_name, target=finding.target, payload=finding.payload,
            pheromone_base=finding.pheromone_base, half_life_sec=finding.half_life_sec,
            embedding=finding.embedding, source_seq=int(source_seq), projection_key=key,
            route_hash=finding.route_hash, route_lineage=finding.route_lineage,
            event_ts=finding.event_ts, projected_at=finding.projected_at,
            pheromone_origin_ts=finding.pheromone_origin_ts,
            fact_origin_ts=finding.fact_origin_ts,
        )
        superseded_ids = tuple(item.finding_id for item in prior if item is not replacement)
        for item in prior:
            if item is not replacement:
                item.superseded_by = replacement.finding_id
        outcome = (ReplacementOutcome.REPLACED if superseded_ids
                   else ReplacementOutcome.INSERTED_NO_PRIOR)
        return ReplacementResult(outcome, replacement, superseded_ids)

    def budget(self, challenge_id: str) -> dict[str, Any]:
        row = self._budgets.setdefault(
            challenge_id,
            {
                "max_agent_hours": 2.0,
                "max_tokens": 2_000_000,
                "used_hours": 0.0,
                "used_tokens": 0,
            },
        )
        return dict(row)

    def update_budget(
        self, challenge_id: str, delta_hours: float = 0.0, delta_tokens: int = 0
    ) -> None:
        row = self._budgets.setdefault(
            challenge_id,
            {
                "max_agent_hours": 2.0,
                "max_tokens": 2_000_000,
                "used_hours": 0.0,
                "used_tokens": 0,
            },
        )
        row["used_hours"] += float(delta_hours)
        row["used_tokens"] += int(delta_tokens)

    def agent_budget(self, challenge_id: str, agent_name: str) -> dict[str, Any]:
        row = self._agent_budgets.setdefault(
            (challenge_id, agent_name),
            {
                "max_tokens": 500_000,
                "warn_at_tokens": 400_000,
                "used_tokens": 0,
                "warned": False,
            },
        )
        return dict(row)

    def charge_agent(
        self, challenge_id: str, agent_name: str, tokens: int = 0
    ) -> None:
        row = self._agent_budgets.setdefault(
            (challenge_id, agent_name),
            {
                "max_tokens": 500_000,
                "warn_at_tokens": 400_000,
                "used_tokens": 0,
                "warned": False,
            },
        )
        row["used_tokens"] += int(tokens)
        if (
            not row["warned"]
            and row["warn_at_tokens"] > 0
            and row["used_tokens"] >= row["warn_at_tokens"]
        ):
            row["warned"] = True
