"""M7 offline energy experiment — pure types, formulas, canonical serialization.

Discipline (docs/10 M7 Contract v9.2, approved 2026-08-16):

- This module NEVER imports ``sqlite3`` or ``shared_graph`` (static assertion
  in tests). Capture lives in ``energy_capture``; persistence in
  ``energy_sidecar``.
- Wall-clock timestamps are used ONLY for decay. Causal membership is seq-based
  and decided in ``energy_capture`` (single-transaction ``MAX(seq)``).
- Canonical record bytes are measured with a fixed-point encoding so the
  measured length equals the real written line length (envelope ``kind``
  included, trailing newline excluded, UTF-8 bytes).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 2 * 1024 * 1024
MAX_RUN_TRACE_BYTES = 256 * 1024 * 1024
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_FIXED_POINT_ITERATIONS = 8

FACT_STATES = frozenset({
    "candidate", "verified", "challenged", "revalidated",
    "rejected", "merged", "superseded",
})
CORRELATION_KINDS = frozenset({"artifact", "fallback"})
OBS_EXCLUSION_REASONS = frozenset({
    "", "missing_route_hash", "non_finite_confidence", "lineage_unresolved",
})
DEAD_END_EXCLUSION_REASONS = frozenset({
    "", "missing_route_hash", "not_applied", "not_genuine_giveup",
})
WORKER_LANES = frozenset({"ordinary", "review"})
PRIORITY_SCALES = frozenset({"planner"})
DECISION_SOURCES = frozenset({"reason"})
LIFECYCLE_STATUSES = frozenset({"in_progress", "finalized"})
DATA_QUALITIES = frozenset({"clean", "incomplete", "corrupt"})
_ENERGY_WEIGHT_KEYS = frozenset({"verified_witness", "verified", "candidate"})


def _finite_float(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number, got bool")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def _req_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _enum_check(value: str, allowed: frozenset[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} has invalid value {value!r}")
    return value


def clamp_finite(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp a numeric value into [low, high]; non-finite input raises."""
    number = _finite_float(value, "value")
    return max(low, min(high, number))


def make_trace_id(run_id: str, instance_id: str, generation: int) -> str:
    """Cross-resume-unique trace identity (docs/10 M7 v6+)."""
    return f"m7-cycle::{run_id}::{instance_id}::{int(generation)}"


def decision_id_for(run_id: str, trace_id: str, original_index: int,
                    intent_id: str, decision_source: str) -> str:
    raw = "|".join([str(run_id), str(trace_id), str(int(original_index)),
                    str(intent_id), str(decision_source)])
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True)
class EnergyConfig:
    weights: Mapping[str, float]
    tau: float = 1800.0
    dead_penalty: float = 0.5
    dead_tau: float = 7200.0

    def __post_init__(self) -> None:
        if not isinstance(self.weights, Mapping):
            raise ValueError("weights must be a mapping")
        copied: dict[str, float] = {}
        for key, value in self.weights.items():
            if key not in _ENERGY_WEIGHT_KEYS:
                raise ValueError(f"unknown energy weight key: {key!r}")
            copied[str(key)] = _finite_float(value, f"weights[{key!r}]")
        missing = _ENERGY_WEIGHT_KEYS - set(copied)
        if missing:
            raise ValueError(f"energy weights missing keys: {sorted(missing)!r}")
        object.__setattr__(self, "weights", MappingProxyType(copied))
        object.__setattr__(self, "tau", _finite_float(self.tau, "tau", minimum=0.0))
        if self.tau <= 0.0:
            raise ValueError("tau must be > 0")
        object.__setattr__(
            self, "dead_penalty", _finite_float(self.dead_penalty, "dead_penalty"))
        if not 0.0 <= self.dead_penalty <= 1.0:
            raise ValueError("dead_penalty must be in [0,1]")
        object.__setattr__(self, "dead_tau", _finite_float(self.dead_tau, "dead_tau"))
        if self.dead_tau <= 0.0:
            raise ValueError("dead_tau must be > 0")


@dataclass(frozen=True)
class EnergyObservationSnapshot:
    fact_seq: int
    fact_origin_ts: float                # VIEW fact_ts (fact_added event ts)
    energy_origin_ts: float              # promotion_ts else fact_ts
    route_hash: str
    lineage: str
    lineage_reason: str
    inherited_intent_ids: tuple[str, ...]
    state: str
    retired: bool
    verified: bool
    base_verified: bool
    confidence: float
    witness: str
    artifact_id: str
    source: str
    actor: str
    correlation_kind: str
    correlation_basis_hash: str
    eligible_for_energy: bool
    exclusion_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.fact_seq, int) or isinstance(self.fact_seq, bool) or self.fact_seq <= 0:
            raise ValueError("fact_seq must be a positive integer")
        object.__setattr__(self, "fact_origin_ts", _finite_float(self.fact_origin_ts, "fact_origin_ts"))
        object.__setattr__(self, "energy_origin_ts", _finite_float(self.energy_origin_ts, "energy_origin_ts"))
        object.__setattr__(self, "state", _enum_check(self.state, FACT_STATES, "state"))
        for name in ("retired", "verified", "base_verified"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        object.__setattr__(self, "confidence", _finite_float(self.confidence, "confidence"))
        object.__setattr__(self, "correlation_kind",
                           _enum_check(self.correlation_kind, CORRELATION_KINDS, "correlation_kind"))
        if not isinstance(self.correlation_basis_hash, str) or not self.correlation_basis_hash:
            raise ValueError("correlation_basis_hash is required")
        if not isinstance(self.eligible_for_energy, bool):
            raise ValueError("eligible_for_energy must be boolean")
        object.__setattr__(self, "exclusion_reason",
                           _enum_check(self.exclusion_reason, OBS_EXCLUSION_REASONS, "exclusion_reason"))


@dataclass(frozen=True)
class DeadEndObservationSnapshot:
    intent_id: str
    route_hash: str
    result_seq: int                      # intents.result_seq (applied conclusion)
    concluded_ts: float                  # applied conclusion event ts
    result: str
    genuine_giveup: bool
    eligible_for_energy: bool
    exclusion_reason: str
    conclusion_event_count: int
    ignored_stale_conclusion_count: int

    def __post_init__(self) -> None:
        _req_str(self.intent_id, "intent_id")
        if not isinstance(self.result_seq, int) or isinstance(self.result_seq, bool) or self.result_seq <= 0:
            raise ValueError("result_seq must be a positive integer")
        object.__setattr__(self, "concluded_ts", _finite_float(self.concluded_ts, "concluded_ts"))
        for name in ("genuine_giveup", "eligible_for_energy"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        object.__setattr__(self, "exclusion_reason",
                           _enum_check(self.exclusion_reason, DEAD_END_EXCLUSION_REASONS, "exclusion_reason"))
        for name in ("conclusion_event_count", "ignored_stale_conclusion_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class GraphCycleSnapshot:
    graph_after_seq: int                 # transaction MAX(seq); sole causal authority
    observations: tuple[EnergyObservationSnapshot, ...]
    dead_ends: tuple[DeadEndObservationSnapshot, ...]
    complete: bool
    exclusion_reason: str
    observed_fact_count: int
    captured_fact_count: int
    stored_fact_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.graph_after_seq, int) or isinstance(self.graph_after_seq, bool) or self.graph_after_seq < 0:
            raise ValueError("graph_after_seq must be a non-negative integer")
        if not isinstance(self.observations, tuple) or not isinstance(self.dead_ends, tuple):
            raise ValueError("observations/dead_ends must be tuples")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be boolean")
        for name in ("observed_fact_count", "captured_fact_count", "stored_fact_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class EnergyDecision:
    decision_id: str
    trace_id: str
    reason_cycle_id: str
    intent_id: str
    route_hash: str
    worker_lane: str
    priority: float
    normalized_priority: float
    priority_scale: str
    original_index: int
    decision_source: str

    def __post_init__(self) -> None:
        for name in ("decision_id", "trace_id", "intent_id"):
            _req_str(getattr(self, name), name)
        object.__setattr__(self, "worker_lane", _enum_check(self.worker_lane, WORKER_LANES, "worker_lane"))
        object.__setattr__(self, "priority", _finite_float(self.priority, "priority"))
        object.__setattr__(self, "normalized_priority",
                           _finite_float(self.normalized_priority, "normalized_priority"))
        object.__setattr__(self, "priority_scale",
                           _enum_check(self.priority_scale, PRIORITY_SCALES, "priority_scale"))
        if not isinstance(self.original_index, int) or isinstance(self.original_index, bool) or self.original_index < 0:
            raise ValueError("original_index must be a non-negative integer")
        object.__setattr__(self, "decision_source",
                           _enum_check(self.decision_source, DECISION_SOURCES, "decision_source"))


@dataclass(frozen=True)
class CycleTrace:
    schema_version: int
    trace_id: str
    reason_cycle_id: str
    decision_ts: float                  # epoch (time.time()); display only
    expected_decision_count: int
    decisions: tuple[EnergyDecision, ...]
    snapshot: GraphCycleSnapshot        # embedded, never hand-copied
    complete: bool
    exclusion_reason: str
    serialized_bytes: int
    serialized_bytes_attempted: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        _req_str(self.trace_id, "trace_id")
        object.__setattr__(self, "decision_ts", _finite_float(self.decision_ts, "decision_ts"))
        if not isinstance(self.expected_decision_count, int) or isinstance(self.expected_decision_count, bool) or self.expected_decision_count < 0:
            raise ValueError("expected_decision_count must be a non-negative integer")
        if not isinstance(self.decisions, tuple):
            raise ValueError("decisions must be a tuple")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be boolean")
        for name in ("serialized_bytes",):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.serialized_bytes_attempted is not None and (
            not isinstance(self.serialized_bytes_attempted, int)
            or isinstance(self.serialized_bytes_attempted, bool)
            or self.serialized_bytes_attempted < 0
        ):
            raise ValueError("serialized_bytes_attempted must be None or a non-negative integer")


class SizeFixedPointError(RuntimeError):
    """Canonical encoding did not converge within MAX_FIXED_POINT_ITERATIONS."""


# ---------------------------------------------------------------------------
# Explicit serialization (no implicit dataclass contract; docs/10 test 28).
# ---------------------------------------------------------------------------

def observation_to_dict(obs: EnergyObservationSnapshot) -> dict[str, Any]:
    return {
        "fact_seq": obs.fact_seq,
        "fact_origin_ts": obs.fact_origin_ts,
        "energy_origin_ts": obs.energy_origin_ts,
        "route_hash": obs.route_hash,
        "lineage": obs.lineage,
        "lineage_reason": obs.lineage_reason,
        "inherited_intent_ids": list(obs.inherited_intent_ids),
        "state": obs.state,
        "retired": obs.retired,
        "verified": obs.verified,
        "base_verified": obs.base_verified,
        "confidence": obs.confidence,
        "witness": obs.witness,
        "artifact_id": obs.artifact_id,
        "source": obs.source,
        "actor": obs.actor,
        "correlation_kind": obs.correlation_kind,
        "correlation_basis_hash": obs.correlation_basis_hash,
        "eligible_for_energy": obs.eligible_for_energy,
        "exclusion_reason": obs.exclusion_reason,
    }


def dead_end_to_dict(dead: DeadEndObservationSnapshot) -> dict[str, Any]:
    return {
        "intent_id": dead.intent_id,
        "route_hash": dead.route_hash,
        "result_seq": dead.result_seq,
        "concluded_ts": dead.concluded_ts,
        "result": dead.result,
        "genuine_giveup": dead.genuine_giveup,
        "eligible_for_energy": dead.eligible_for_energy,
        "exclusion_reason": dead.exclusion_reason,
        "conclusion_event_count": dead.conclusion_event_count,
        "ignored_stale_conclusion_count": dead.ignored_stale_conclusion_count,
    }


def decision_to_dict(decision: EnergyDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "trace_id": decision.trace_id,
        "reason_cycle_id": decision.reason_cycle_id,
        "intent_id": decision.intent_id,
        "route_hash": decision.route_hash,
        "worker_lane": decision.worker_lane,
        "priority": decision.priority,
        "normalized_priority": decision.normalized_priority,
        "priority_scale": decision.priority_scale,
        "original_index": decision.original_index,
        "decision_source": decision.decision_source,
    }


def snapshot_to_dict(snapshot: GraphCycleSnapshot) -> dict[str, Any]:
    return {
        "graph_after_seq": snapshot.graph_after_seq,
        "observations": [observation_to_dict(o) for o in snapshot.observations],
        "dead_ends": [dead_end_to_dict(d) for d in snapshot.dead_ends],
        "complete": snapshot.complete,
        "exclusion_reason": snapshot.exclusion_reason,
        "observed_fact_count": snapshot.observed_fact_count,
        "captured_fact_count": snapshot.captured_fact_count,
        "stored_fact_count": snapshot.stored_fact_count,
    }


def _trace_row(trace: CycleTrace, serialized_bytes: int) -> dict[str, Any]:
    """Canonical record: kind envelope + all CycleTrace fields."""
    return {
        "kind": "cycle_trace",
        "schema_version": trace.schema_version,
        "trace_id": trace.trace_id,
        "reason_cycle_id": trace.reason_cycle_id,
        "decision_ts": trace.decision_ts,
        "expected_decision_count": trace.expected_decision_count,
        "decisions": [decision_to_dict(d) for d in trace.decisions],
        "snapshot": snapshot_to_dict(trace.snapshot),
        "complete": trace.complete,
        "exclusion_reason": trace.exclusion_reason,
        "serialized_bytes": serialized_bytes,
        "serialized_bytes_attempted": trace.serialized_bytes_attempted,
    }


def encode_cycle_trace_line(trace: CycleTrace) -> tuple[bytes, int]:
    """Fixed-point canonical encoding.

    Returns (line_bytes_without_newline, stable_length). The serialized_bytes
    field is iterated until its own digit count stabilizes, so the measured
    length equals the real written line length.
    """
    current = 0
    for _ in range(MAX_FIXED_POINT_ITERATIONS + 1):
        row = _trace_row(trace, current)
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        length = len(payload.encode("utf-8"))
        if length == current:
            return payload.encode("utf-8"), length
        current = length
    raise SizeFixedPointError("cycle_trace canonical encoding did not converge")


def build_size_stub(trace: CycleTrace, *, full_attempted_bytes: int) -> CycleTrace:
    """Oversize stub: keep decisions+metadata+counts, drop observations/dead_ends."""
    snapshot = trace.snapshot
    stripped = GraphCycleSnapshot(
        graph_after_seq=snapshot.graph_after_seq,
        observations=(),
        dead_ends=(),
        complete=False,
        exclusion_reason="snapshot_size_limit",
        observed_fact_count=snapshot.observed_fact_count,
        captured_fact_count=snapshot.captured_fact_count,
        stored_fact_count=0,
    )
    return CycleTrace(
        schema_version=trace.schema_version,
        trace_id=trace.trace_id,
        reason_cycle_id=trace.reason_cycle_id,
        decision_ts=trace.decision_ts,
        expected_decision_count=trace.expected_decision_count,
        decisions=trace.decisions,
        snapshot=stripped,
        complete=False,
        exclusion_reason="snapshot_size_limit",
        serialized_bytes=0,
        serialized_bytes_attempted=full_attempted_bytes,
    )


def cycle_trace_from_row(row: dict[str, Any]) -> CycleTrace:
    """Reconstruct a CycleTrace from a canonical sidecar record."""
    snap_row = row["snapshot"]
    observations = tuple(
        EnergyObservationSnapshot(
            fact_seq=int(o["fact_seq"]),
            fact_origin_ts=float(o["fact_origin_ts"]),
            energy_origin_ts=float(o["energy_origin_ts"]),
            route_hash=str(o["route_hash"]),
            lineage=str(o["lineage"]),
            lineage_reason=str(o["lineage_reason"]),
            inherited_intent_ids=tuple(str(i) for i in o["inherited_intent_ids"]),
            state=str(o["state"]),
            retired=bool(o["retired"]),
            verified=bool(o["verified"]),
            base_verified=bool(o["base_verified"]),
            confidence=float(o["confidence"]),
            witness=str(o["witness"]),
            artifact_id=str(o["artifact_id"]),
            source=str(o["source"]),
            actor=str(o["actor"]),
            correlation_kind=str(o["correlation_kind"]),
            correlation_basis_hash=str(o["correlation_basis_hash"]),
            eligible_for_energy=bool(o["eligible_for_energy"]),
            exclusion_reason=str(o["exclusion_reason"]),
        )
        for o in snap_row["observations"])
    dead_ends = tuple(
        DeadEndObservationSnapshot(
            intent_id=str(d["intent_id"]),
            route_hash=str(d["route_hash"]),
            result_seq=int(d["result_seq"]),
            concluded_ts=float(d["concluded_ts"]),
            result=str(d["result"]),
            genuine_giveup=bool(d["genuine_giveup"]),
            eligible_for_energy=bool(d["eligible_for_energy"]),
            exclusion_reason=str(d["exclusion_reason"]),
            conclusion_event_count=int(d["conclusion_event_count"]),
            ignored_stale_conclusion_count=int(d["ignored_stale_conclusion_count"]),
        )
        for d in snap_row["dead_ends"])
    snapshot = GraphCycleSnapshot(
        graph_after_seq=int(snap_row["graph_after_seq"]),
        observations=observations,
        dead_ends=dead_ends,
        complete=bool(snap_row["complete"]),
        exclusion_reason=str(snap_row["exclusion_reason"]),
        observed_fact_count=int(snap_row["observed_fact_count"]),
        captured_fact_count=int(snap_row["captured_fact_count"]),
        stored_fact_count=int(snap_row["stored_fact_count"]),
    )
    decisions = tuple(
        EnergyDecision(
            decision_id=str(d["decision_id"]),
            trace_id=str(d["trace_id"]),
            reason_cycle_id=str(d["reason_cycle_id"]),
            intent_id=str(d["intent_id"]),
            route_hash=str(d["route_hash"]),
            worker_lane=str(d["worker_lane"]),
            priority=float(d["priority"]),
            normalized_priority=float(d["normalized_priority"]),
            priority_scale=str(d["priority_scale"]),
            original_index=int(d["original_index"]),
            decision_source=str(d["decision_source"]),
        )
        for d in row["decisions"])
    return CycleTrace(
        schema_version=int(row.get("schema_version", SCHEMA_VERSION)),
        trace_id=str(row["trace_id"]),
        reason_cycle_id=str(row["reason_cycle_id"]),
        decision_ts=float(row["decision_ts"]),
        expected_decision_count=int(row["expected_decision_count"]),
        decisions=decisions,
        snapshot=snapshot,
        complete=bool(row["complete"]),
        exclusion_reason=str(row["exclusion_reason"]),
        serialized_bytes=int(row["serialized_bytes"]),
        serialized_bytes_attempted=row.get("serialized_bytes_attempted"),
    )


def decision_id_matches(decision: EnergyDecision, run_id: str) -> bool:
    expected = decision_id_for(run_id, decision.trace_id, decision.original_index,
                               decision.intent_id, decision.decision_source)
    return decision.decision_id == expected


def validate_cycle_trace(trace: CycleTrace, *, run_id: str) -> list[str]:
    """Structural trace validator (docs/10: three-layer complete invariants)."""
    errors: list[str] = []
    if trace.schema_version != SCHEMA_VERSION:
        errors.append("schema_version")
    if not trace.trace_id.strip():
        errors.append("trace_id")
    if trace.expected_decision_count != len(trace.decisions):
        errors.append("expected_decision_count mismatch")
    for decision in trace.decisions:
        if not decision_id_matches(decision, run_id):
            errors.append(f"decision_id mismatch: {decision.intent_id}")
            break
    if trace.snapshot.stored_fact_count != len(trace.snapshot.observations):
        errors.append("stored_fact_count != len(observations)")
    return errors


# ---------------------------------------------------------------------------
# M7-1 formulas (docs/10: route_energies + reorder_decisions, three orderings).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteEnergy:
    route_hash: str
    positive: float
    penalty: float
    energy: float
    flag_captured: bool
    raw_fact_count: int
    correlation_group_count: int
    eligible: bool


def _tier_of(obs: EnergyObservationSnapshot) -> str:
    if obs.verified and bool(obs.witness.strip()):
        return "verified_witness"
    if obs.verified:
        return "verified"
    return "candidate"


def route_energies(
    observations: Sequence[EnergyObservationSnapshot],
    dead_ends: Sequence[DeadEndObservationSnapshot],
    config: EnergyConfig,
    *,
    as_of_ts: float,
    captured_routes: frozenset[str] = frozenset(),
) -> dict[str, RouteEnergy]:
    """Per-route energy. Membership is decided by capture (seq cutoff); here
    ``as_of_ts`` is used ONLY for decay and never for membership (v8/v9 time
    model)."""
    as_of = _finite_float(as_of_ts, "as_of_ts")
    weights = config.weights

    # census per route (challenged counted, retired excluded — docs/10 test 86)
    census: dict[str, int] = {}
    # contributing observations grouped by route -> correlation basis hash
    groups: dict[str, dict[str, list[float]]] = {}
    for obs in observations:
        if not obs.eligible_for_energy or obs.retired:
            continue
        if not math.isfinite(obs.confidence):
            continue
        route = obs.route_hash
        if not route:
            continue
        census[route] = census.get(route, 0) + 1
        if obs.state == "challenged":
            continue  # no positive contribution
        confidence = clamp_finite(obs.confidence)
        raw_score = weights[_tier_of(obs)] * confidence
        decayed = clamp_finite(raw_score) * math.exp(
            -max(0.0, as_of - obs.energy_origin_ts) / config.tau)
        groups.setdefault(route, {}).setdefault(
            obs.correlation_basis_hash, []).append(decayed)

    # dead-end penalties (positive max merge per route)
    penalties: dict[str, float] = {}
    for dead in dead_ends:
        if not dead.eligible_for_energy or not dead.route_hash:
            continue
        penalty = config.dead_penalty * math.exp(
            -max(0.0, as_of - dead.concluded_ts) / config.dead_tau)
        penalties[dead.route_hash] = max(penalties.get(dead.route_hash, 0.0), penalty)

    universe = set(groups) | set(penalties) | set(captured_routes)
    out: dict[str, RouteEnergy] = {}
    for route in sorted(universe):
        group_scores = groups.get(route, {})
        positive = 1.0
        group_count = 0
        for scores in group_scores.values():
            if not scores:
                continue
            positive *= (1.0 - max(scores))
            group_count += 1
        positive = clamp_finite(1.0 - positive)
        penalty = clamp_finite(penalties.get(route, 0.0))
        energy = clamp_finite(positive - penalty)
        out[route] = RouteEnergy(
            route_hash=route,
            positive=positive,
            penalty=penalty,
            energy=energy,
            flag_captured=route in captured_routes,
            raw_fact_count=census.get(route, 0),
            correlation_group_count=group_count,
            eligible=positive > 0.0,
        )
    return out


def _lane_rank(decision: EnergyDecision) -> int:
    return 1 if decision.worker_lane == "review" else 0


def _scale_rank(decision: EnergyDecision) -> int:
    return 0 if decision.priority_scale == "operator" else 1


def planner_baseline_order(
    decisions: Sequence[EnergyDecision],
) -> list[EnergyDecision]:
    """Sort mirroring the dispatchable queue semantics (lane, scale, -priority,
    FIFO). production_order is simply the input order."""
    return sorted(
        decisions,
        key=lambda d: (_lane_rank(d), _scale_rank(d), -d.normalized_priority,
                       d.original_index),
    )


def energy_order(
    decisions: Sequence[EnergyDecision],
    energies: Mapping[str, RouteEnergy],
) -> list[EnergyDecision]:
    """planner_baseline_order + -energy within exact-equal groups only."""
    def key(d: EnergyDecision):
        route = energies.get(d.route_hash) if d.route_hash else None
        energy = route.energy if route is not None else 0.0
        return (_lane_rank(d), _scale_rank(d), -d.normalized_priority,
                -energy, d.original_index)
    return sorted(decisions, key=key)


def reorder_decisions(
    decisions: Sequence[EnergyDecision],
    *,
    enabled: bool,
    energy_supplier: Callable[[], Mapping[str, RouteEnergy]],
) -> list[EnergyDecision]:
    """Three orderings (docs/10 v8): production_order is the input order;
    enabled=False returns production_order and NEVER calls the supplier."""
    if not enabled:
        return list(decisions)
    energies = energy_supplier()
    if not energies:
        return planner_baseline_order(decisions)
    return energy_order(decisions, energies)
